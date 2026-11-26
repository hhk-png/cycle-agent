#!/usr/bin/env bash
# Logical backup: Postgres dump + object-store mirror.
# Usage: BACKUP_DIR=/srv/backups ./backup.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -f "${SCRIPT_DIR}/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  . "${SCRIPT_DIR}/.env"
  set +a
fi

BACKUP_DIR="${BACKUP_DIR:-./backups}"
mkdir -p "${BACKUP_DIR}"
BACKUP_DIR="$(cd "${BACKUP_DIR}" && pwd)"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
DEST="${BACKUP_DIR}/${STAMP}"
BUCKET="${MLFLOW_BUCKET:-mlflow}"

mkdir -p "${DEST}"

echo "[1/2] postgres dump -> ${DEST}/mlflow.dump"
docker compose exec -T postgres \
  pg_dump -U "${POSTGRES_USER:-mlflow}" -d "${POSTGRES_DB:-mlflow}" -Fc \
  > "${DEST}/mlflow.dump"

echo "[2/2] artifact mirror -> ${DEST}/artifacts"
docker compose run --rm -v "${DEST}:/backup" --entrypoint /bin/sh minio-init -c "
  mc alias set local http://minio:9000 '${MINIO_ROOT_USER:-minioadmin}' '${MINIO_ROOT_PASSWORD:-change-me-minio}' &&
  mc mirror --overwrite local/${BUCKET} /backup/artifacts
"

echo "backup complete: ${DEST}"
echo "verify before deleting anything: pg_restore --list ${DEST}/mlflow.dump | head"
