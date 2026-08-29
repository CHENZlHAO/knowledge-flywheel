#!/usr/bin/env bash
# Read-only production readiness report. Never changes data.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT/knowledge-hub"

BASE_URL="${BASE_URL:-http://127.0.0.1:8000}"
AUTH="${ADMIN_API_KEY:-}"

fetch() {
  local path="$1"
  if [[ -n "$AUTH" ]]; then
    curl -fsS -H "X-Admin-Key: $AUTH" "${BASE_URL}${path}"
  else
    curl -fsS "${BASE_URL}${path}"
  fi
}

echo "== Health"
fetch /healthz || true
echo
echo "== Executor"
fetch /api/v1/executor/health || true
echo
echo "== Orchestration"
fetch /api/v1/orchestration/status || true
echo
echo "== Security"
fetch /api/v1/security/status || true
echo
echo "== Alert delivery summary"
fetch /api/v1/alert-deliveries | python3 -c 'import json,sys; d=json.load(sys.stdin); print(json.dumps(d["summary"], ensure_ascii=False))' 2>/dev/null || true
echo
echo "== Replica policy health"
fetch /api/v1/replica-policy/health || true
echo
echo "== Backup runs"
fetch /api/v1/admin/backup/status | python3 -c 'import json,sys; d=json.load(sys.stdin); print("runs:", len(d.get("runs", [])), "dir:", d.get("backup_dir"))' 2>/dev/null || true
echo

# Environment key coverage check (no secrets printed).
echo "== Environment key coverage"
python3 - <<'PY'
from app.config import settings
required = {
    "admin_api_key": settings.admin_api_key,
    "node_api_key": settings.node_api_key,
    "security_secret": settings.security_secret,
    "mqtt_bridge_api_key": settings.mqtt_bridge_api_key,
}
missing = [name for name, value in required.items() if not value]
if settings.app_env == "production":
    for name, value in required.items():
        print(f"  {name}: {'set' if value else 'MISSING'}")
    if missing:
        print("  PRODUCTION WARNING: missing", ", ".join(missing))
else:
    print("  environment:", settings.app_env, "(bootstrap keys optional in development)")
PY
