#!/usr/bin/env bash
# Batch-distribute the Windows edge agent to a list of employee machines.
#
#   scripts/deploy-edge-batch.sh nodes.txt
#
# nodes.txt lines:  <windows-host>  [node-id]
# The EXE must already be built (scripts/build-all.sh) and code-signed.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NODES_FILE="${1:?usage: deploy-edge-batch.sh <nodes.txt>}"
AGENT_EXE="$ROOT/knowledge-edge-agent/dist/knowledge-edge-agent.exe"
INSTALL_BAT="$ROOT/knowledge-edge-agent/scripts/windows/install-service.bat"

[[ -f "$AGENT_EXE" ]] || { echo "missing $AGENT_EXE; run scripts/build-all.sh first" >&2; exit 1; }
[[ -f "$NODES_FILE" ]] || { echo "missing node list: $NODES_FILE" >&2; exit 1; }

while read -r host node_id _; do
  [[ -n "$host" ]] || continue
  echo "==> Deploying to ${host} (node-id=${node_id:-auto})"
  scp "$AGENT_EXE" "$INSTALL_BAT" "${host}:/C:/KnowledgeEdge/" 2>/dev/null || {
    echo "scp failed for ${host}; push the signed EXE + install-service.bat manually" >&2
    continue
  }
  # The service is installed/started by an administrator on the target machine:
  #   C:\KnowledgeEdge\install-service.bat
done < "$NODES_FILE"

echo "Batch distribution complete. Install each service as Administrator on the target."
