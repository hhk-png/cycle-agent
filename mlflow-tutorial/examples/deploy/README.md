# Reference deployment stack

This directory is a **reference**, not a production-certified distribution.
The shell scripts assume a POSIX shell; on Windows run them from WSL2 or a
Linux host (bind-mount paths and `docker compose run -v` differ on cmd.exe).
It shows the minimum topology that keeps metadata and artifacts consistent:

```text
client -> nginx (TLS) -> mlflow server (2 workers)
                              |            |
                         postgres        minio (S3 API)
```

## What is exercised by the tutorial's CI

- Compose YAML parsing, required services/flags, and Nginx references
  (`python scripts/check_deploy.py`);
- shell syntax of `backup.sh` / `restore.sh` when `bash` is available;
- MiniMLflow's own tests, examples, and documentation checks.

## What is **not** exercised here

- `docker compose up` (no Docker daemon in the tutorial CI);
- TLS certificates, DNS, firewalls, and OIDC;
- Postgres/S3 behavior under load;
- backup/restore timing and retention.

Run the stack on a disposable machine and complete the checklist in the
chapter before treating any of it as production.

## Quick trial

```bash
cp .env.example .env
# edit .env: replace every password
mkdir -p certs
openssl req -x509 -newkey rsa:2048 -nodes -days 30 \
  -keyout certs/server.key -out certs/server.crt \
  -subj "/CN=localhost"
docker compose up -d --build
curl -k https://localhost:8443/health
```

The UI is at <https://localhost:8443>. The server initializes the schema at
startup; for an existing database run the migration explicitly before
starting the new version:

```bash
# .env is read by Compose, not by your interactive shell.
set -a; . ./.env; set +a
docker compose run --rm --entrypoint mlflow mlflow db upgrade \
  "postgresql://${POSTGRES_USER}:${POSTGRES_PASSWORD}@postgres:5432/${POSTGRES_DB}"
```

## Backup and restore

`backup.sh` / `restore.sh` source `.env` themselves.

```bash
BACKUP_DIR=./backups ./backup.sh
# practice the restore on a copy, never on the only production database
BACKUP_DIR=./backups/20260101T000000Z ./restore.sh
```

Pin `MINIO_VERSION` / `MLFLOW_VERSION` in `.env` before production; the compose
file's `latest` tags are for a local trial.
