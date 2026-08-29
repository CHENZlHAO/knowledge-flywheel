#!/usr/bin/env bash
# Build both deliverables: the center container set and the Windows edge EXE.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VERSION="${1:-0.1.0}"

echo "==> Validating center Compose config"
(cd "$ROOT/knowledge-hub" && docker compose config >/dev/null)

echo "==> Building center images (no start)"
(cd "$ROOT/knowledge-hub" && docker compose build postgres redis emqx hub worker)

echo "==> Building Windows amd64 edge agent (version ${VERSION})"
(cd "$ROOT/knowledge-edge-agent" && go mod tidy && CGO_ENABLED=0 GOOS=windows GOARCH=amd64 go build -trimpath -ldflags "-s -w -X main.agentVersion=${VERSION}" -o dist/knowledge-edge-agent.exe .)

echo "==> Artifacts"
ls -lh "$ROOT/knowledge-edge-agent/dist/knowledge-edge-agent.exe"
echo "Center images are built; run scripts/deploy-center.sh to start them."
