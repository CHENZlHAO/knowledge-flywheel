#!/usr/bin/env bash
# Restore a center backup run produced by scripts/backup.sh.
#
#   scripts/restore.sh <backup_id> [--db-only|--storage-only]
#
# The default restores PostgreSQL and the object-storage root. Restore is a
# destructive operation: it stops the API/worker, restores data, then restarts.
set -euo pipefail

BACKUP_ID="${1:?usage: restore.sh <backup_id> [--db-only|--storage-only]}"
MODE="${2:-all}"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT/knowledge-hub"

RUN_DIR="$(cd "$ROOT" && pwd)/knowledge-hub/backups/${BACKUP_ID}"
if [[ ! -f "${RUN_DIR}/manifest.json" ]]; then
  echo "backup run not found: ${RUN_DIR}" >&2
  exit 2
fi

echo "Restoring backup ${BACKUP_ID} (mode=${MODE})" >&2
docker compose stop hub worker

if [[ "${MODE}" != "db-only" ]]; then
  STORAGE_ROOT="${STORAGE_ROOT:-./storage}"
  echo "Restoring object storage to ${STORAGE_ROOT}" >&2
  tar -xzf "${RUN_DIR}/storage.tar.gz" -C .
fi

if [[ "${MODE}" != "storage-only" ]]; then
  echo "Restoring PostgreSQL from ${RUN_DIR}/database.dump" >&2
  # The custom-format dump is restored into the running postgres container.
  docker compose up -d postgres
  docker compose exec -T postgres sh -c 'dropdb --if-exists -U knowledge knowledge && createdb -U knowledge knowledge' || true
  docker compose exec -T postgres pg_restore -U knowledge -d knowledge --clean --if-exists - < "${RUN_DIR}/database.dump"
fi

echo "Restarting center services" >&2
docker compose up -d hub worker
echo "Restore complete; verify /healthz and then run scripts/inspect-production.sh" >&2
