#!/bin/sh
set -eu

ENV_FILE=${ENV_FILE:-.env.production}
BACKUP_ROOT=${BACKUP_ROOT:?Set BACKUP_ROOT to a canonical absolute directory outside the repository}
COMPOSE_PROJECT_NAME=${COMPOSE_PROJECT_NAME:-frame-intelligence-production}
COMPOSE_FILE=${COMPOSE_FILE:-compose.production.yaml}
env_value() { sed -n "s/^$1=//p" "$ENV_FILE" | tail -n 1; }
POSTGRES_USER=$(env_value POSTGRES_USER)
POSTGRES_DB=$(env_value POSTGRES_DB)
MINIO_BUCKET=$(env_value MINIO_BUCKET)

case "$BACKUP_ROOT" in /*) ;; *) echo "BACKUP_ROOT must be an absolute path." >&2; exit 1;; esac
test -d "$BACKUP_ROOT" && test ! -L "$BACKUP_ROOT" || { echo "BACKUP_ROOT must be an existing non-symlink directory." >&2; exit 1; }
canonical_root=$(realpath "$BACKUP_ROOT")
test "$canonical_root" = "$BACKUP_ROOT" || { echo "BACKUP_ROOT must already be canonical." >&2; exit 1; }
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
repository=$(realpath "$script_dir/../..")
test -f "$COMPOSE_FILE" && test ! -L "$COMPOSE_FILE" || { echo "COMPOSE_FILE must be a regular non-symlink file." >&2; exit 1; }
canonical_compose=$(realpath "$COMPOSE_FILE")
expected_compose="$repository/compose.production.yaml"
test "$canonical_compose" = "$expected_compose" && test -f "$expected_compose" && test ! -L "$expected_compose" || { echo "COMPOSE_FILE must identify the repository production Compose file." >&2; exit 1; }
case "$canonical_root" in "$repository"|"$repository"/*) echo "BACKUP_ROOT must be outside the repository." >&2; exit 1;; esac
chmod 700 "$canonical_root"

timestamp=$(date -u +%Y%m%dT%H%M%SZ)
temporary=$(mktemp -d "$canonical_root/.partial-${timestamp}-XXXXXXXX")
chmod 700 "$temporary"
final="$canonical_root/${timestamp}-${temporary##*-}"
cleanup() { test -n "${temporary:-}" && test -d "$temporary" && rm -rf -- "$temporary"; }
trap cleanup EXIT INT TERM HUP
mkdir -m 700 "$temporary/minio"

compose() { docker compose --project-name "$COMPOSE_PROJECT_NAME" --env-file "$ENV_FILE" -f "$COMPOSE_FILE" "$@"; }
compose exec -T postgres pg_dump --format=custom --no-owner --no-acl --username "$POSTGRES_USER" "$POSTGRES_DB" > "$temporary/postgres.dump"
compose exec -T postgres pg_restore --list < "$temporary/postgres.dump" >/dev/null
compose run --rm --no-deps --entrypoint /bin/sh -v "$temporary/minio:/backup" minio-init -ec '
  mc alias set source http://minio:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" >/dev/null
  mc mirror --overwrite "source/$MINIO_BUCKET" /backup >/dev/null
'
postgres_version=$(compose exec -T postgres postgres --version | tr -d '\r')
minio_client_version=$(compose run --rm --no-deps --entrypoint /bin/sh minio-init -ec 'mc --version | head -n 1' | tr -d '\r')
{
  printf 'FORMAT_VERSION=1\n'
  printf 'SOURCE_PROJECT=%s\n' "$COMPOSE_PROJECT_NAME"
  printf 'SOURCE_DATABASE=%s\n' "$POSTGRES_DB"
  printf 'SOURCE_BUCKET=%s\n' "$MINIO_BUCKET"
  printf 'POSTGRES_VERSION=%s\n' "$postgres_version"
  printf 'MINIO_CLIENT_VERSION=%s\n' "$minio_client_version"
} > "$temporary/METADATA"
(cd "$temporary" && find . -type l -exec false {} +)
(cd "$temporary" && find . -type f ! -name SHA256SUMS ! -name COMPLETE -print0 | LC_ALL=C sort -z | xargs -0 sha256sum > SHA256SUMS)
(cd "$temporary" && sha256sum -c SHA256SUMS >/dev/null)
printf 'complete\n' > "$temporary/COMPLETE"
mv -T --no-clobber "$temporary" "$final"
test ! -d "$temporary" || { echo "Refusing to overwrite an existing backup." >&2; exit 1; }
temporary=
trap - EXIT INT TERM HUP
echo "Backup completed in the configured external backup root: ${final##*/}"
