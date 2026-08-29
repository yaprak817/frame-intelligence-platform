#!/bin/sh
set -eu

ENV_FILE=${1:-.env.production}
test -f "$ENV_FILE" || { echo "Environment file is missing." >&2; exit 1; }

env_value() {
  count=$(grep -c "^$1=" "$ENV_FILE" || true)
  test "$count" = 1 || { echo "$1 must occur exactly once." >&2; exit 1; }
  sed -n "s/^$1=//p" "$ENV_FILE"
}

required='APP_SITE STORAGE_SITE OBJECT_STORAGE_EXTERNAL_ENDPOINT POSTGRES_DB POSTGRES_USER POSTGRES_PASSWORD JOB_SOURCE_ENCRYPTION_KEY INTERNAL_PROXY_SHARED_SECRET MINIO_ROOT_USER MINIO_ROOT_PASSWORD MINIO_BUCKET'
for name in $required; do
  value=$(env_value "$name")
  test -n "$value" || { echo "$name is required." >&2; exit 1; }
  case "$value" in
    *[[:space:]\"\'\`\\\$\{\}]*|*replace*|*change_me*|*changeme*|*password*|*.invalid*)
      echo "$name contains an unsafe or placeholder value." >&2; exit 1 ;;
  esac
done

validate_site() {
  name=$1
  value=$(env_value "$name")
  case "$value" in https://*) host=${value#https://} ;; *) echo "$name must use HTTPS." >&2; exit 1;; esac
  case "$host" in *[:/@?#]*|localhost|*localhost*|\**|*[!A-Za-z0-9.-]*) echo "$name must contain only a public DNS hostname without a port or path." >&2; exit 1;; esac
  case "$host" in *..*) echo "$name hostname contains an empty label." >&2; exit 1;; esac
  test "${#host}" -le 253 || { echo "$name hostname is too long." >&2; exit 1; }
  printf '%s\n' "$host" | grep -Eq '^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$' || { echo "$name hostname is invalid." >&2; exit 1; }
  printf '%s\n' "$host" | grep -Eq '\.' || { echo "$name must not be a single-label hostname." >&2; exit 1; }
  printf '%s\n' "$host" | grep -Eq '(^|\.)[0-9]+(\.[0-9]+){3}$' && { echo "$name must not be an IP address." >&2; exit 1; }
  old_ifs=$IFS; IFS=.; set -- $host; IFS=$old_ifs
  for label in "$@"; do
    test -n "$label" && test "${#label}" -le 63 || { echo "$name hostname label is invalid." >&2; exit 1; }
    case "$label" in -*|*-) echo "$name hostname label is invalid." >&2; exit 1;; esac
  done
}

validate_site APP_SITE
validate_site STORAGE_SITE
app_site=$(env_value APP_SITE)
storage_site=$(env_value STORAGE_SITE)
external=$(env_value OBJECT_STORAGE_EXTERNAL_ENDPOINT)
test "$app_site" != "$storage_site" || { echo "APP_SITE and STORAGE_SITE must differ." >&2; exit 1; }
test "$external" = "$storage_site" || { echo "OBJECT_STORAGE_EXTERNAL_ENDPOINT must exactly equal STORAGE_SITE." >&2; exit 1; }

for name in POSTGRES_PASSWORD MINIO_ROOT_PASSWORD INTERNAL_PROXY_SHARED_SECRET; do
  value=$(env_value "$name")
  test "${#value}" -ge 32 || { echo "$name must contain at least 32 characters." >&2; exit 1; }
done

key=$(env_value JOB_SOURCE_ENCRYPTION_KEY)
printf '%s' "$key" | grep -Eq '^[A-Za-z0-9_-]{43}=$' || { echo "JOB_SOURCE_ENCRYPTION_KEY must be padded URL-safe base64." >&2; exit 1; }
decoded_key=$(mktemp)
trap 'rm -f -- "$decoded_key"' EXIT INT TERM HUP
printf '%s' "$key" | tr '_-' '/+' | base64 -d > "$decoded_key" 2>/dev/null || { echo "JOB_SOURCE_ENCRYPTION_KEY must decode successfully." >&2; exit 1; }
decoded_size=$(wc -c < "$decoded_key" | tr -d ' ')
test "$decoded_size" = 32 || { echo "JOB_SOURCE_ENCRYPTION_KEY must decode to exactly 32 bytes." >&2; exit 1; }
canonical_key=$(base64 --wrap=0 "$decoded_key") || { echo "JOB_SOURCE_ENCRYPTION_KEY could not be re-encoded." >&2; exit 1; }
canonical_key=$(printf '%s' "$canonical_key" | tr '+/' '-_')
test "$canonical_key" = "$key" || { echo "JOB_SOURCE_ENCRYPTION_KEY must use canonical padded URL-safe base64." >&2; exit 1; }
rm -f -- "$decoded_key"
trap - EXIT INT TERM HUP

echo "Production environment contract is valid."
