#!/usr/bin/env bash
# One-shot center backup (database dump + object-storage archive + manifest).
# Uses the running hub container so it shares DATABASE_URL and STORAGE_ROOT.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT/knowledge-hub"

if docker compose ps --status running hub >/dev/null 2>&1; then
  docker compose exec -T hub python -c 'from app.backup import perform_backup; import json; print(json.dumps(perform_backup(), ensure_ascii=False, indent=2))'
else
  echo "hub service is not running; starting a one-off backup container" >&2
  docker compose run --rm hub python -c 'from app.backup import perform_backup; import json; print(json.dumps(perform_backup(), ensure_ascii=False, indent=2))'
fi
