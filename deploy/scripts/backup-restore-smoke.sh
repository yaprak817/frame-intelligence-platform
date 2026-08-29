#!/bin/sh
set -eu

project=${BACKUP_SMOKE_PROJECT:-framebackupsmoke_$(date -u +%Y%m%d%H%M%S)}
printf '%s\n' "$project" | grep -Eq '^framebackupsmoke_[a-z0-9][a-z0-9_]{7,48}$' || { echo "Unsafe backup smoke project name." >&2; exit 1; }
root=$(mktemp -d)
env_file="$root/fixture.env"
restore_env_file="$root/restore-fixture.env"
backup_root="$root/backups"
mkdir -p "$backup_root"

cleanup() {
  docker compose --project-name "$project" --env-file "$env_file" -f compose.production.yaml down --volumes --remove-orphans --timeout 10 >/dev/null 2>&1 || true
  rm -rf "$root"
  echo "CONTAINERS=$(docker ps -aq --filter "label=com.docker.compose.project=$project" | wc -l | tr -d ' ')"
  echo "NETWORKS=$(docker network ls -q --filter "label=com.docker.compose.project=$project" | wc -l | tr -d ' ')"
  echo "VOLUMES=$(docker volume ls -q --filter "label=com.docker.compose.project=$project" | wc -l | tr -d ' ')"
  test ! -e "$root" && echo "TEMP_DIRS=0"
}
trap cleanup EXIT INT TERM

cat > "$env_file" <<'EOF'
APP_SITE=https://app.example.test
STORAGE_SITE=https://storage.example.test
OBJECT_STORAGE_EXTERNAL_ENDPOINT=https://storage.example.test
POSTGRES_DB=backup_fixture
POSTGRES_USER=backup_fixture_user
POSTGRES_PASSWORD=backup_fixture_password_32_chars
JOB_SOURCE_ENCRYPTION_KEY=JSUlJSUlJSUlJSUlJSUlJSUlJSUlJSUlJSUlJSUlJSU=
INTERNAL_PROXY_SHARED_SECRET=fixture_proxy_secret_0123456789ABCDEF
MINIO_ROOT_USER=backup_fixture_access
MINIO_ROOT_PASSWORD=backup_fixture_password_32_chars
MINIO_BUCKET=backup-fixture
EOF
sed 's/POSTGRES_DB=backup_fixture/POSTGRES_DB=restore_fixture/; s/MINIO_BUCKET=backup-fixture/MINIO_BUCKET=restore-fixture/' "$env_file" > "$restore_env_file"

export APP_SITE=https://app.example.test
export STORAGE_SITE=https://storage.example.test
export OBJECT_STORAGE_EXTERNAL_ENDPOINT=https://storage.example.test
export POSTGRES_DB=backup_fixture
export POSTGRES_USER=backup_fixture_user
export POSTGRES_PASSWORD=backup_fixture_password_32_chars
export JOB_SOURCE_ENCRYPTION_KEY=JSUlJSUlJSUlJSUlJSUlJSUlJSUlJSUlJSUlJSUlJSU=
export INTERNAL_PROXY_SHARED_SECRET=fixture_proxy_secret_0123456789ABCDEF
export MINIO_ROOT_USER=backup_fixture_access
export MINIO_ROOT_PASSWORD=backup_fixture_password_32_chars
export MINIO_BUCKET=backup-fixture

compose() { docker compose --project-name "$project" --env-file "$env_file" -f compose.production.yaml "$@"; }
refresh_manifest() {
  (cd "$1" && find . -type f ! -name SHA256SUMS ! -name COMPLETE -print0 | LC_ALL=C sort -z | xargs -0 sha256sum > SHA256SUMS)
}
compose up -d --wait postgres minio
compose run --rm minio-init

# A failed backup may leave no final or partial backup artifact behind.
compose stop --timeout 10 postgres
if ENV_FILE="$env_file" BACKUP_ROOT="$backup_root" COMPOSE_PROJECT_NAME="$project" sh deploy/scripts/backup.sh >/dev/null 2>&1; then
  echo "Backup unexpectedly succeeded while PostgreSQL was unavailable." >&2; exit 1
fi
test -z "$(find "$backup_root" -mindepth 1 -maxdepth 1 -print -quit)" || { echo "Failed backup left an artifact." >&2; exit 1; }
compose start postgres
compose exec -T postgres sh -ec 'until pg_isready --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" >/dev/null; do sleep 1; done'

compose exec -T postgres psql --username backup_fixture_user --dbname backup_fixture --set ON_ERROR_STOP=1 --command "CREATE TABLE restore_probe(value text NOT NULL); INSERT INTO restore_probe VALUES ('expected');" >/dev/null
compose run --rm --no-deps --entrypoint /bin/sh minio-init -ec '
  printf expected > /tmp/probe
  mc alias set target http://minio:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" >/dev/null
  mc cp /tmp/probe "target/$MINIO_BUCKET/probe" >/dev/null
'

ENV_FILE="$env_file" BACKUP_ROOT="$backup_root" COMPOSE_PROJECT_NAME="$project" sh deploy/scripts/backup.sh
backup_dir=$(find "$backup_root" -mindepth 1 -maxdepth 1 -type d | head -n 1)
test -n "$backup_dir"
test "$(stat -c %a "$backup_root")" = 700
test "$(stat -c %a "$backup_dir")" = 700
test "$(cat "$backup_dir/COMPLETE")" = complete
(cd "$backup_dir" && sha256sum --strict -c SHA256SUMS >/dev/null)
compose exec -T postgres pg_restore --list < "$backup_dir/postgres.dump" >/dev/null
case "$backup_dir" in "$PWD"|"$PWD"/*) echo "Backup was created inside the repository." >&2; exit 1;; esac
expected_hash=$(sha256sum "$backup_dir/minio/probe" | awk '{print $1}')
expected_size=$(wc -c < "$backup_dir/minio/probe" | tr -d ' ')
test "$expected_size" = 8

# Checksum failure must occur before either target is modified.
tampered="$root/tampered-backup"
cp -a "$backup_dir" "$tampered"
printf 'tampered\n' >> "$tampered/postgres.dump"
if ENV_FILE="$restore_env_file" BACKUP_DIR="$tampered" COMPOSE_PROJECT_NAME="$project" RESTORE_CONFIRMATION="$project:restore_fixture:restore-fixture" sh deploy/scripts/restore.sh >/dev/null 2>&1; then
  echo "Restore accepted a corrupted checksum." >&2; exit 1
fi

# The manifest must describe exactly the backup tree and may not escape it.
unlisted="$root/unlisted-backup"
cp -a "$backup_dir" "$unlisted"
printf 'not-listed\n' > "$unlisted/minio/unlisted"
if ENV_FILE="$restore_env_file" BACKUP_DIR="$unlisted" COMPOSE_PROJECT_NAME="$project" RESTORE_CONFIRMATION="$project:restore_fixture:restore-fixture" sh deploy/scripts/restore.sh >/dev/null 2>&1; then
  echo "Restore accepted an unlisted backup file." >&2; exit 1
fi
traversal="$root/traversal-backup"
cp -a "$backup_dir" "$traversal"
printf '%064d  ../outside\n' 0 >> "$traversal/SHA256SUMS"
if ENV_FILE="$restore_env_file" BACKUP_DIR="$traversal" COMPOSE_PROJECT_NAME="$project" RESTORE_CONFIRMATION="$project:restore_fixture:restore-fixture" sh deploy/scripts/restore.sh >/dev/null 2>&1; then
  echo "Restore accepted a manifest path outside the backup." >&2; exit 1
fi
test ! -e "$root/outside"

nonregular="$root/nonregular-backup"
cp -a "$backup_dir" "$nonregular"
mkfifo "$nonregular/minio/not-a-regular-file"
if ENV_FILE="$restore_env_file" BACKUP_DIR="$nonregular" COMPOSE_PROJECT_NAME="$project" RESTORE_CONFIRMATION="$project:restore_fixture:restore-fixture" sh deploy/scripts/restore.sh >/dev/null 2>&1; then
  echo "Restore accepted non-regular backup content." >&2; exit 1
fi

bad_metadata="$root/bad-metadata-backup"
cp -a "$backup_dir" "$bad_metadata"
sed -i 's/^FORMAT_VERSION=1$/FORMAT_VERSION=2/' "$bad_metadata/METADATA"
refresh_manifest "$bad_metadata"
if ENV_FILE="$restore_env_file" BACKUP_DIR="$bad_metadata" COMPOSE_PROJECT_NAME="$project" RESTORE_CONFIRMATION="$project:restore_fixture:restore-fixture" sh deploy/scripts/restore.sh >/dev/null 2>&1; then
  echo "Restore accepted an unsupported metadata format." >&2; exit 1
fi

duplicate_metadata="$root/duplicate-metadata-backup"
cp -a "$backup_dir" "$duplicate_metadata"
printf 'SOURCE_PROJECT=duplicate\n' >> "$duplicate_metadata/METADATA"
refresh_manifest "$duplicate_metadata"
if ENV_FILE="$restore_env_file" BACKUP_DIR="$duplicate_metadata" COMPOSE_PROJECT_NAME="$project" RESTORE_CONFIRMATION="$project:restore_fixture:restore-fixture" sh deploy/scripts/restore.sh >/dev/null 2>&1; then
  echo "Restore accepted duplicate metadata." >&2; exit 1
fi

# GNU mv's no-clobber publish primitive must preserve an existing final artifact.
mkdir "$root/publish-source" "$root/publish-target"
printf 'source\n' > "$root/publish-source/marker"
printf 'target\n' > "$root/publish-target/marker"
mv -T --no-clobber "$root/publish-source" "$root/publish-target" || true
test "$(cat "$root/publish-target/marker")" = target
test "$(cat "$root/publish-source/marker")" = source

compose exec -T postgres createdb --username backup_fixture_user restore_fixture
compose run --rm --no-deps --entrypoint /bin/sh minio-init -ec '
  mc alias set target http://minio:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" >/dev/null
  mc mb "target/restore-fixture" >/dev/null
'

# Source and target identities must differ even with exact confirmation.
if ENV_FILE="$env_file" BACKUP_DIR="$backup_dir" COMPOSE_PROJECT_NAME="$project" RESTORE_CONFIRMATION="$project:backup_fixture:backup-fixture" sh deploy/scripts/restore.sh >/dev/null 2>&1; then
  echo "Restore accepted its source as the target." >&2; exit 1
fi
same_database_env="$root/same-database.env"
sed 's/MINIO_BUCKET=backup-fixture/MINIO_BUCKET=other-fixture/' "$env_file" > "$same_database_env"
if ENV_FILE="$same_database_env" BACKUP_DIR="$backup_dir" COMPOSE_PROJECT_NAME="$project" RESTORE_CONFIRMATION="$project:backup_fixture:other-fixture" sh deploy/scripts/restore.sh >/dev/null 2>&1; then
  echo "Restore accepted the source database as its target." >&2; exit 1
fi
same_bucket_env="$root/same-bucket.env"
sed 's/POSTGRES_DB=backup_fixture/POSTGRES_DB=other_fixture/' "$env_file" > "$same_bucket_env"
if ENV_FILE="$same_bucket_env" BACKUP_DIR="$backup_dir" COMPOSE_PROJECT_NAME="$project" RESTORE_CONFIRMATION="$project:other_fixture:backup-fixture" sh deploy/scripts/restore.sh >/dev/null 2>&1; then
  echo "Restore accepted the source bucket as its target." >&2; exit 1
fi

if ENV_FILE="$restore_env_file" BACKUP_DIR="$backup_dir" COMPOSE_PROJECT_NAME="$project" sh deploy/scripts/restore.sh >/dev/null 2>&1; then
  echo "Restore unexpectedly ran without explicit target confirmation." >&2
  exit 1
fi
compose exec -T postgres psql --username backup_fixture_user --dbname restore_fixture --set ON_ERROR_STOP=1 --command "CREATE TABLE occupied(value text);" >/dev/null
if ENV_FILE="$restore_env_file" BACKUP_DIR="$backup_dir" COMPOSE_PROJECT_NAME="$project" RESTORE_CONFIRMATION="$project:restore_fixture:restore-fixture" sh deploy/scripts/restore.sh >/dev/null 2>&1; then
  echo "Restore accepted a non-empty database." >&2; exit 1
fi
compose exec -T postgres psql --username backup_fixture_user --dbname restore_fixture --set ON_ERROR_STOP=1 --command "DROP TABLE occupied;" >/dev/null
compose run --rm --no-deps --entrypoint /bin/sh minio-init -ec '
  mc alias set target http://minio:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" >/dev/null
  printf occupied >/tmp/occupied
  mc cp /tmp/occupied "target/restore-fixture/occupied" >/dev/null
'
if ENV_FILE="$restore_env_file" BACKUP_DIR="$backup_dir" COMPOSE_PROJECT_NAME="$project" RESTORE_CONFIRMATION="$project:restore_fixture:restore-fixture" sh deploy/scripts/restore.sh >/dev/null 2>&1; then
  echo "Restore accepted a non-empty bucket." >&2; exit 1
fi
compose run --rm --no-deps --entrypoint /bin/sh minio-init -ec '
  mc alias set target http://minio:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" >/dev/null
  mc rm "target/restore-fixture/occupied" >/dev/null
'
export POSTGRES_DB=restore_fixture
export MINIO_BUCKET=restore-fixture
ENV_FILE="$restore_env_file" BACKUP_DIR="$backup_dir" COMPOSE_PROJECT_NAME="$project" RESTORE_CONFIRMATION="$project:restore_fixture:restore-fixture" sh deploy/scripts/restore.sh
test "$(compose exec -T postgres psql --tuples-only --no-align --username backup_fixture_user --dbname restore_fixture --command 'SELECT value FROM restore_probe;')" = expected
compose run --rm --no-deps --entrypoint /bin/sh minio-init -ec '
  mc alias set target http://minio:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" >/dev/null
  mc cat "target/$MINIO_BUCKET/probe"
' > "$root/restored-probe"
test "$(wc -c < "$root/restored-probe" | tr -d ' ')" = "$expected_size"
test "$(sha256sum "$root/restored-probe" | awk '{print $1}')" = "$expected_hash"
echo "Isolated PostgreSQL and MinIO backup/restore smoke passed."
