#!/bin/sh
set -eu

root=$(mktemp -d)
cleanup() { rm -rf -- "$root"; }
trap cleanup EXIT INT TERM HUP

valid="$root/valid.env"
cat > "$valid" <<'EOF'
APP_SITE=https://app.example.test
STORAGE_SITE=https://storage.example.test
OBJECT_STORAGE_EXTERNAL_ENDPOINT=https://storage.example.test
POSTGRES_DB=fixture_db
POSTGRES_USER=fixture_user
POSTGRES_PASSWORD=0123456789abcdef0123456789ABCDEF
JOB_SOURCE_ENCRYPTION_KEY=JSUlJSUlJSUlJSUlJSUlJSUlJSUlJSUlJSUlJSUlJSU=
INTERNAL_PROXY_SHARED_SECRET=fixture_proxy_secret_0123456789ABCDEF
MINIO_ROOT_USER=fixture_access
MINIO_ROOT_PASSWORD=ABCDEF0123456789abcdef0123456789
MINIO_BUCKET=fixture-bucket
EOF
sh deploy/scripts/validate-env.sh "$valid" >/dev/null

for replacement in \
  's#POSTGRES_PASSWORD=.*#POSTGRES_PASSWORD=short#' \
  's#JOB_SOURCE_ENCRYPTION_KEY=.*#JOB_SOURCE_ENCRYPTION_KEY=invalid#' \
  's#JOB_SOURCE_ENCRYPTION_KEY=.*#JOB_SOURCE_ENCRYPTION_KEY=JSUlJSUlJSUlJSUlJSUlJSUlJSUlJSUlJSUlJSUlJSV=#' \
  's#JOB_SOURCE_ENCRYPTION_KEY=.*##' \
  's#INTERNAL_PROXY_SHARED_SECRET=.*#INTERNAL_PROXY_SHARED_SECRET=short#' \
  's#APP_SITE=.*#APP_SITE=https://127.0.0.1#' \
  's#APP_SITE=.*#APP_SITE=https://localhost#' \
  's#APP_SITE=.*#APP_SITE=https://app.example.test/path#' \
  's#APP_SITE=.*#APP_SITE=https://user@app.example.test#' \
  's#APP_SITE=.*#APP_SITE=https://app.example.test:443#' \
  's#APP_SITE=.*#APP_SITE=https://app..example.test#' \
  's#APP_SITE=.*#APP_SITE=https://app.example.test?debug=1#' \
  's#APP_SITE=.*#APP_SITE=https://app.example.test\#debug#' \
  's#APP_SITE=.*#APP_SITE=https://*.example.test#' \
  's#POSTGRES_PASSWORD=.*#POSTGRES_PASSWORD=$${INJECTED_VALUE}#' \
  's#STORAGE_SITE=.*#STORAGE_SITE=https://app.example.test#' \
  's#OBJECT_STORAGE_EXTERNAL_ENDPOINT=.*#OBJECT_STORAGE_EXTERNAL_ENDPOINT=https://other.example.test#'; do
  invalid="$root/invalid.env"
  sed "$replacement" "$valid" > "$invalid"
  if sh deploy/scripts/validate-env.sh "$invalid" >/dev/null 2>&1; then
    echo "Invalid production environment was accepted: $replacement" >&2; exit 1
  fi
done

grep -Fq 'INTERNAL_PROXY_SHARED_SECRET: ${INTERNAL_PROXY_SHARED_SECRET:?required}' compose.production.yaml
test "$(grep -F 'networks: [frontend-edge, application]' compose.production.yaml | wc -l | tr -d ' ')" = 1
test "$(grep -F 'networks: [storage-edge, data]' compose.production.yaml | wc -l | tr -d ' ')" = 1
test "$(grep -F 'networks: [data]' compose.production.yaml | wc -l | tr -d ' ')" -ge 6
grep -Fq 'response_header_timeout 7230s' deploy/Caddyfile
grep -Fq 'response_header_timeout 330s' deploy/Caddyfile
grep -Fq 'BACKUP_ROOT=/srv/frame-intelligence-backups COMPOSE_PROJECT_NAME=frame-intelligence-production ENV_FILE=.env.production COMPOSE_FILE=compose.production.yaml sh deploy/scripts/backup.sh' docs/production-operations.md

mkdir "$root/external"
printf 'services: {}\n' > "$root/unexpected-compose.yaml"
if BACKUP_ROOT="$root/external" COMPOSE_FILE="$root/unexpected-compose.yaml" ENV_FILE="$valid" sh deploy/scripts/backup.sh >/dev/null 2>&1; then
  echo "Unexpected Compose file was accepted." >&2; exit 1
fi
ln -s "$PWD/compose.production.yaml" "$root/compose-link.yaml"
if BACKUP_ROOT="$root/external" COMPOSE_FILE="$root/compose-link.yaml" ENV_FILE="$valid" sh deploy/scripts/backup.sh >/dev/null 2>&1; then
  echo "Symlink Compose file was accepted." >&2; exit 1
fi
if BACKUP_ROOT=backups ENV_FILE="$valid" sh deploy/scripts/backup.sh >/dev/null 2>&1; then
  echo "Relative backup root was accepted." >&2; exit 1
fi
if BACKUP_ROOT="$PWD" ENV_FILE="$valid" sh deploy/scripts/backup.sh >/dev/null 2>&1; then
  echo "Repository backup root was accepted." >&2; exit 1
fi
repository=$PWD
if (cd "$root" && BACKUP_ROOT="$root/external/../external" ENV_FILE="$valid" sh "$repository/deploy/scripts/backup.sh" >/dev/null 2>&1); then
  echo "A path containing a non-canonical parent component was accepted." >&2; exit 1
fi
ln -s "$root/external" "$root/link"
if BACKUP_ROOT="$root/link" ENV_FILE="$valid" sh deploy/scripts/backup.sh >/dev/null 2>&1; then
  echo "Symlink backup root was accepted." >&2; exit 1
fi
if ln -s "$PWD" "$root/repository-link" 2>/dev/null; then
  if BACKUP_ROOT="$root/repository-link" ENV_FILE="$valid" sh deploy/scripts/backup.sh >/dev/null 2>&1; then
    echo "A symlink into the repository was accepted." >&2; exit 1
  fi
fi

echo "Production environment and backup path contracts passed."
