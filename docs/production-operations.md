# Production operations

This deployment targets one Ubuntu Docker host without depending on a cloud provider. Caddy is the only public entry point. It terminates HTTPS, redirects HTTP automatically, sends the application domain to the Next.js frontend, and sends the storage domain to private MinIO. The frontend retains the same-origin `/api/v1` proxy to the private backend.

> Authentication and job ownership are not implemented. Do not expose this stack to the Internet until an explicit access policy, upstream authentication/WAF, abuse controls, upload quotas, and incident response ownership exist.

The examples consistently use project `frame-intelligence-production`, `.env.production`, and `compose.production.yaml`. Do not omit these selectors or reuse them for a restore target.

## Initial setup and start

Install a supported Docker Engine with Compose v2, configure separate application and storage DNS names, and allow inbound TCP 80/443 only. Copy `.env.production.example` to the ignored `.env.production`. Generate independent passwords of at least 32 random characters, an independent `INTERNAL_PROXY_SHARED_SECRET` of at least 32 random characters, and `JOB_SOURCE_ENCRYPTION_KEY` as padded URL-safe base64 encoding of 32 random bytes. Never pass secrets as build arguments or place them in command history.

```sh
sh deploy/scripts/validate-env.sh .env.production
docker compose --project-name frame-intelligence-production --env-file .env.production -f compose.production.yaml config --quiet
docker compose --project-name frame-intelligence-production --env-file .env.production -f compose.production.yaml up -d --build --wait
docker compose --project-name frame-intelligence-production --env-file .env.production -f compose.production.yaml ps
curl --fail --silent https://frames.example.com/api/v1/live
curl --fail --silent https://frames.example.com/api/v1/ready
```

Caddy obtains and renews certificates in `caddy_data`; its config state is in `caddy_config`. Access logs are disabled because storage requests may contain presigned credentials in query strings. The upload proxy timeout defaults to 1800 seconds and is bounded to 60–7200 seconds with `UPLOAD_PROXY_TIMEOUT_SECONDS`; ordinary API calls retain their separate 1–300 second bounded timeout. Caddy allows 7230 seconds for an upload response and 330 seconds for other application responses, leaving a small cleanup margin beyond the corresponding maximum Next.js deadlines.

## Routine operations

```sh
# Bounded logs
docker compose --project-name frame-intelligence-production --env-file .env.production -f compose.production.yaml logs --tail 200 frontend backend outbox-publisher frame-worker migrate

# Explicit migration; the database advisory lock has a bounded wait.
docker compose --project-name frame-intelligence-production --env-file .env.production -f compose.production.yaml run --rm migrate

# Safe update: create and verify a backup first, then update reviewed source/images.
BACKUP_ROOT=/srv/frame-intelligence-backups COMPOSE_PROJECT_NAME=frame-intelligence-production ENV_FILE=.env.production COMPOSE_FILE=compose.production.yaml sh deploy/scripts/backup.sh
docker compose --project-name frame-intelligence-production --env-file .env.production -f compose.production.yaml build --pull
docker compose --project-name frame-intelligence-production --env-file .env.production -f compose.production.yaml run --rm migrate
docker compose --project-name frame-intelligence-production --env-file .env.production -f compose.production.yaml up -d --wait

# Stop or remove containers while retaining named volumes.
docker compose --project-name frame-intelligence-production --env-file .env.production -f compose.production.yaml stop
docker compose --project-name frame-intelligence-production --env-file .env.production -f compose.production.yaml down

# Disk/volume inspection.
docker compose --project-name frame-intelligence-production --env-file .env.production -f compose.production.yaml exec postgres du -sh /var/lib/postgresql/data
docker compose --project-name frame-intelligence-production --env-file .env.production -f compose.production.yaml exec minio du -sh /data
docker volume ls --filter label=com.docker.compose.project=frame-intelligence-production
```

Never add `--volumes` to a production `down` and never use system-wide prune commands. Diagnose with the explicit `ps`, bounded logs, `/live`, `/ready`, disk capacity, DNS/certificate state, migration exit status, and dependency health.

## Backup and restore

`BACKUP_ROOT` must be an existing canonical absolute non-symlink directory outside the repository. Keep an encrypted second copy off the Docker host. A backup is written to a private temporary directory, verified with `pg_restore --list` and SHA-256 checksums, marked complete, then atomically renamed.

```sh
BACKUP_ROOT=/srv/frame-intelligence-backups \
COMPOSE_PROJECT_NAME=frame-intelligence-production \
ENV_FILE=.env.production \
COMPOSE_FILE=compose.production.yaml \
sh deploy/scripts/backup.sh
```

Restore is destructive only to a deliberately new, empty target. Create a separate environment file whose project, database and bucket names do not identify the source. Both the database and bucket must be empty; the script checks this before mutation. Use an exact confirmation:

```sh
BACKUP_DIR=/srv/frame-intelligence-backups/20260829T120000Z-example \
COMPOSE_PROJECT_NAME=frame-intelligence-restore-check \
ENV_FILE=.env.restore-check \
COMPOSE_FILE=compose.production.yaml \
RESTORE_CONFIRMATION=frame-intelligence-restore-check:restore_check_db:restore-check-bucket \
sh deploy/scripts/restore.sh
```

PostgreSQL restore uses one transaction, but PostgreSQL plus MinIO together cannot form one cross-system transaction. A MinIO failure after PostgreSQL commits can therefore leave a partial cross-system restore. Limiting restore to a new/empty isolated target makes that failure recoverable: discard only the isolated target and retry. Never restore directly over production or development data.

Redis is deliberately ephemeral: it is only a broker/rate-limit cache. Durable job/outbox state is PostgreSQL and queued work is recovered from the transactional outbox.

## Rollback

Keep the reviewed pre-update revision/image references and a verified off-host backup. Return to that reviewed revision, rebuild with the exact selectors below, run only schema-compatible migrations, and start the stack. Database downgrade is not automatic; if it is required, restore into a separately named empty target and verify it before cutover.

```sh
docker compose --project-name frame-intelligence-production --env-file .env.production -f compose.production.yaml build
docker compose --project-name frame-intelligence-production --env-file .env.production -f compose.production.yaml run --rm migrate
docker compose --project-name frame-intelligence-production --env-file .env.production -f compose.production.yaml up -d --wait
```
