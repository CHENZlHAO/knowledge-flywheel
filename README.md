# Enterprise Knowledge Flywheel

This repository contains two independently versioned deliverables:

- `knowledge-hub`: center service, PostgreSQL schema, operator console, and worker integration points.
- `knowledge-edge-agent`: small Go binary for Windows/macOS edge nodes.

The implementation is intentionally operationally conservative. PostgreSQL owns metadata/task state, center-managed replica nodes are the authoritative file store, and edge nodes report state and manifests. AI and Dify adapters are proposal-only until an operator approves a change.

Beyond the MVP baseline the codebase now also ships: deterministic LangGraph orchestration (`register → parse → embed → replica`, with a dependency-free sequential fallback), a DeepSeek-Harness review adapter (proposal-only, deterministic fallback), binary object storage (local filesystem or S3/MinIO), external alert delivery (email/webhook/WeCom/DingTalk with retry/dead-letter/suppression), RBAC with database-backed API-key rotation + JWTs + an OIDC hook, backup/restore with manifests, a native Windows Service mode for the edge agent, and a self-contained stock Dify deployment reference. See the docs for each adapter's boundary and what still requires external infrastructure/credentials to verify end-to-end.

## Quick start

```bash
cd knowledge-hub
cp .env.example .env
docker compose up --build
```

Open `http://localhost:8000/` for the operator console and `http://localhost:8000/docs` for the API contract.

After the stack is running, execute the host-side acceptance flow:

```bash
make acceptance-smoke
```

It verifies health, node heartbeat, idempotent file reporting, deterministic UTF-8 parsing/chunking, worker success/failure state transitions, and the reserved mobile command queue/claim/ACK contract. It does not claim that edge-disk reads, Syncthing, MQTT delivery, vector generation, Dify, or model adapters are production-ready.

The current replica slice validates fixed replica targets, records file/node holdings, dispatches idempotent `replica_repair` work as edge `sync_replica` commands, and requires a matching SHA-256 ACK before recording success. The Agent rejects unsafe paths and can verify bytes already present under its watch directory. It does not copy missing bytes until the Syncthing adapter is installed.

Mobile clients can use the reserved control-plane endpoints under `/api/v1/mobile`: overview, node list, remote command queue, and command status. Remote commands currently return `adapter_pending`; an MQTT/Agent execution adapter is the next integration step.

For local development without Docker:

```bash
./scripts/setup-python.sh
source knowledge-hub/.venv/bin/activate
DATABASE_URL=sqlite:///./dev.db uvicorn app.main:app --reload
```

Run tests later with `make test`. The virtual environment is stored under `knowledge-hub/.venv` and is ignored by Git.
If the official PyPI endpoint is slow or blocked, setup automatically retries with the Tsinghua mirror; override it with `PIP_FALLBACK_INDEX=<index-url>`.

## Production constraints

- Use Linux containers as the primary production target. Windows servers must run the Linux Compose stack through WSL2 or a Linux VM.
- Use MQTT over TLS (`8883`) with per-node credentials in production; `1883` is development-only.
- Put the API behind HTTPS and SSO/RBAC before exposing it outside a trusted management VLAN.
- Configure backups and perform a restore drill before onboarding business data. The baseline does not claim zero data loss or 100% availability.

See `knowledge-hub/docs/operations.md` and `knowledge-hub/docs/architecture.md` for the complete operating model and acceptance gates.

Go installation and Windows cross-compilation instructions are in `knowledge-edge-agent/BUILD-MAC.md` and `knowledge-edge-agent/BUILD-WINDOWS.md`.
