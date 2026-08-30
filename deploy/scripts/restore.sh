#!/bin/sh
set -eu

ENV_FILE=${ENV_FILE:-.env.production}
BACKUP_DIR=${BACKUP_DIR:?Set BACKUP_DIR to the canonical absolute backup directory}
COMPOSE_PROJECT_NAME=${COMPOSE_PROJECT_NAME:-frame-intelligence-production}
COMPOSE_FILE=${COMPOSE_FILE:-compose.production.yaml}
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
repository=$(realpath "$script_dir/../..")
test -f "$COMPOSE_FILE" && test ! -L "$COMPOSE_FILE" || { echo "COMPOSE_FILE must be a regular non-symlink file." >&2; exit 1; }
canonical_compose=$(realpath "$COMPOSE_FILE")
expected_compose="$repository/compose.production.yaml"
test "$canonical_compose" = "$expected_compose" && test -f "$expected_compose" && test ! -L "$expected_compose" || { echo "COMPOSE_FILE must identify the repository production Compose file." >&2; exit 1; }
env_value() { sed -n "s/^$1=//p" "$ENV_FILE" | tail -n 1; }
metadata_value() { sed -n "s/^$1=//p" "$BACKUP_DIR/METADATA" | tail -n 1; }
POSTGRES_USER=$(env_value POSTGRES_USER)
POSTGRES_DB=$(env_value POSTGRES_DB)
MINIO_BUCKET=$(env_value MINIO_BUCKET)
expected="${COMPOSE_PROJECT_NAME}:${POSTGRES_DB}:${MINIO_BUCKET}"
test "${RESTORE_CONFIRMATION:-}" = "$expected" || {
  echo "Refusing restore. Set RESTORE_CONFIRMATION to the exact project:database:bucket target." >&2
  exit 1
}

case "$BACKUP_DIR" in /*) ;; *) echo "BACKUP_DIR must be absolute." >&2; exit 1;; esac
test -d "$BACKUP_DIR" && test ! -L "$BACKUP_DIR" || { echo "Invalid backup directory." >&2; exit 1; }
test "$(realpath "$BACKUP_DIR")" = "$BACKUP_DIR" || { echo "BACKUP_DIR must already be canonical." >&2; exit 1; }
test -f "$BACKUP_DIR/postgres.dump" && test -f "$BACKUP_DIR/METADATA" && test -f "$BACKUP_DIR/SHA256SUMS" && test -f "$BACKUP_DIR/COMPLETE" || { echo "Backup is incomplete." >&2; exit 1; }
test "$(cat "$BACKUP_DIR/COMPLETE")" = complete || { echo "Backup completion marker is invalid." >&2; exit 1; }
test -z "$(find "$BACKUP_DIR" ! -type d ! -type f -print -quit)" || { echo "Backup may contain only directories and regular files." >&2; exit 1; }
actual_manifest=$(mktemp)
cleanup_manifest() { rm -f -- "$actual_manifest"; }
trap cleanup_manifest EXIT INT TERM HUP
(cd "$BACKUP_DIR" && find . -type f ! -name SHA256SUMS ! -name COMPLETE -print0 | LC_ALL=C sort -z | xargs -0 sha256sum > "$actual_manifest")
cmp -s "$BACKUP_DIR/SHA256SUMS" "$actual_manifest" || { echo "Backup checksum inventory does not exactly match its files." >&2; exit 1; }
(cd "$BACKUP_DIR" && sha256sum --strict -c SHA256SUMS >/dev/null) || { echo "Backup checksum verification failed." >&2; exit 1; }
cleanup_manifest
trap - EXIT INT TERM HUP

test "$(wc -l < "$BACKUP_DIR/METADATA" | tr -d ' ')" = 6 || { echo "Backup metadata has an invalid field count." >&2; exit 1; }
if LC_ALL=C grep -q '[[:cntrl:]]' "$BACKUP_DIR/METADATA"; then
  echo "Backup metadata contains control characters." >&2; exit 1
fi
for name in FORMAT_VERSION SOURCE_PROJECT SOURCE_DATABASE SOURCE_BUCKET POSTGRES_VERSION MINIO_CLIENT_VERSION; do
  test "$(grep -c "^$name=" "$BACKUP_DIR/METADATA" || true)" = 1 || { echo "Backup metadata fields are invalid." >&2; exit 1; }
  test -n "$(metadata_value "$name")" || { echo "Backup metadata fields are incomplete." >&2; exit 1; }
done
test "$(metadata_value FORMAT_VERSION)" = 1 || { echo "Unsupported backup metadata format." >&2; exit 1; }
source_project=$(metadata_value SOURCE_PROJECT)
source_database=$(metadata_value SOURCE_DATABASE)
source_bucket=$(metadata_value SOURCE_BUCKET)
test -n "$source_project" && test -n "$source_database" && test -n "$source_bucket" || { echo "Backup source metadata is incomplete." >&2; exit 1; }
test "$source_database" != "$POSTGRES_DB" || { echo "Source and target databases must differ." >&2; exit 1; }
test "$source_bucket" != "$MINIO_BUCKET" || { echo "Source and target buckets must differ." >&2; exit 1; }

compose() { docker compose --project-name "$COMPOSE_PROJECT_NAME" --env-file "$ENV_FILE" -f "$COMPOSE_FILE" "$@"; }
relation_count=$(compose exec -T postgres psql --tuples-only --no-align --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" --set ON_ERROR_STOP=1 --command "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname NOT IN ('pg_catalog','information_schema') AND c.relkind IN ('r','p','v','m','S','f');" | tr -d '[:space:]')
test "$relation_count" = 0 || { echo "Target database is not empty." >&2; exit 1; }
compose run --rm --no-deps --entrypoint /bin/sh minio-init -ec '
  mc alias set target http://minio:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" >/dev/null
  if mc stat "target/$MINIO_BUCKET" >/dev/null 2>&1; then
    test -z "$(mc ls --recursive "target/$MINIO_BUCKET")" || { echo "Target bucket is not empty." >&2; exit 1; }
  fi
'

compose exec -T postgres pg_restore --single-transaction --no-owner --no-acl --exit-on-error --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" < "$BACKUP_DIR/postgres.dump"
compose run --rm --no-deps --entrypoint /bin/sh -v "$BACKUP_DIR/minio:/backup:ro" minio-init -ec '
  mc alias set target http://minio:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" >/dev/null
  mc mb --ignore-existing "target/$MINIO_BUCKET" >/dev/null
  test -z "$(mc ls --recursive "target/$MINIO_BUCKET")"
  mc mirror --overwrite /backup "target/$MINIO_BUCKET" >/dev/null
'
echo "Restore completed for the explicitly confirmed new and empty target."
