#!/usr/bin/env bash
set -euo pipefail

root_dir="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
test -f "$root_dir/knowledge-hub/docker-compose.yml"
test -f "$root_dir/knowledge-hub/migrations/001_initial.sql"
test -f "$root_dir/knowledge-edge-agent/go.mod"
test -f "$root_dir/knowledge-edge-agent/main.go"
grep -q 'idempotency_key' "$root_dir/knowledge-hub/app/models.py"
grep -q 'review_proposal' "$root_dir/knowledge-hub/app/main.py"
grep -q '/api/v1/knowledge/search' "$root_dir/knowledge-hub/app/main.py"
grep -q 'embedding vector(1024)' "$root_dir/knowledge-hub/migrations/001_initial.sql"
grep -q 'sha256' "$root_dir/knowledge-edge-agent/main.go"
echo "contract-check-ok"
