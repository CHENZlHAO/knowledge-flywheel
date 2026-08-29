#!/usr/bin/env bash
# One-click center deployment: validate config, build images, start services,
# and print the operator URLs. Safe to rerun.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT/knowledge-hub"

if [[ ! -f .env ]]; then
  echo "knowledge-hub/.env is missing; run scripts/init-production-env.sh first." >&2
  exit 1
fi

echo "==> Validating Compose config"
docker compose config >/dev/null

echo "==> Building and starting center services"
docker compose up -d --build postgres redis emqx hub worker

echo "==> Waiting for /healthz"
for _ in $(seq 1 60); do
  if curl -fsS http://127.0.0.1:8000/healthz >/dev/null 2>&1; then
    break
  fi
  sleep 2
done
curl -fsS http://127.0.0.1:8000/healthz

echo
echo "Center is up:"
echo "  console: http://127.0.0.1:8000/"
echo "  api docs: http://127.0.0.1:8000/docs"
echo "  health: http://127.0.0.1:8000/healthz"
