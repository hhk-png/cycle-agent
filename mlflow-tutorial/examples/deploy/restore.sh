#!/usr/bin/env bash
# Restore a backup produced by backup.sh into an empty stack.
# Usage: BACKUP_DIR=./backups/20260101T000000Z ./restore.sh
# This is destructive: it drops and recreates the MLflow database.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -f "${SCRIPT_DIR}/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  . "${SCRIPT_DIR}/.env"
  set +a
fi

: "${BACKUP_DIR:?set BACKUP_DIR to the directory produced by backup.sh}"
DUMP="${BACKUP_DIR}/mlflow.dump"
BUCKET="${MLFLOW_BUCKET:-mlflow}"

test -f "${DUMP}" || { echo "missing ${DUMP}" >&2; exit 1; }

echo "[1/2] recreate database"
docker compose exec -T postgres psql -U "${POSTGRES_USER:-mlflow}" -d postgres -v ON_ERROR_STOP=1 <<SQL
DROP DATABASE IF EXISTS "${POSTGRES_DB:-mlflow}" WITH (FORCE);
CREATE DATABASE "${POSTGRES_DB:-mlflow}" OWNER "${POSTGRES_USER:-mlflow}";
SQL

echo "[2/2] restore dump"
docker compose exec -T postgres \
  pg_restore -U "${POSTGRES_USER:-mlflow}" -d "${POSTGRES_DB:-mlflow}" \
  --no-owner --clean --if-exists < "${DUMP}"

echo "restore complete; start mlflow and run the smoke checks"
echo "note: restore artifacts separately with 'mc mirror ${BACKUP_DIR}/artifacts local/${BUCKET}'"
