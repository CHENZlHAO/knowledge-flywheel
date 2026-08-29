# Architecture and boundaries

## Ownership

PostgreSQL is the source of truth for metadata, state transitions, approvals, and audit records. The center-managed fixed replica pool is the authoritative file store. Edge nodes are reporting/working caches and must not issue business deletes.

MQTT carries edge heartbeat and event telemetry only. In the secure overlay it uses mutual TLS, per-node credentials, topic ACLs, and retained last-will status. HTTP application APIs are the only business mutation path; the bridge can call only the dedicated authenticated node-status endpoint. Queue delivery is at-least-once; consumers use idempotency keys and leases. Celery messages contain only a persisted task ID and are published after the database commit. The database poller remains the recovery path when Redis or Celery is unavailable.

LangGraph owns business workflow orchestration. Celery (or the worker adapter) owns execution capacity. A model or Dify adapter can only create a proposal or task result; it cannot directly mutate knowledge data.

## Required production adapters

The baseline exposes stable HTTP contracts for node heartbeats, file reports, tasks, proposal reviews, protected flywheel event ingestion, and separately keyed text/binary download gateways. The optional Celery delivery adapter reuses the deterministic database state machine. Code-level production adapters are now present for LangGraph orchestration, DeepSeek-Harness review, binary object storage, external alert delivery, RBAC/JWT/API-key identity, and backup/restore. Live external verification (real Syncthing byte transfer, a running Dify instance, an OIDC IdP/MFA, SMTP/WeCom/DingTalk credentials, Ollama models, and a Windows signing certificate) still requires the corresponding infrastructure and credentials.

## Orchestration boundary

`ORCHESTRATION_MODE=state_machine` (default) runs the deterministic
`register -> parse -> embed -> replica` pipeline sequentially. Setting
`ORCHESTRATION_MODE=langgraph` (after `pip install -r requirements-langgraph.txt`)
compiles the same four nodes into a real LangGraph `StateGraph`. Both modes use
the identical node functions and PostgreSQL as the source of truth; the optional
package is never a hard runtime dependency. The graph is invoked through the
`pipeline_run` task kind, and the individual stage tasks remain for
backward-compatible, resumable execution.

## Object storage boundary

UTF-8 text keeps its existing parse path. Binary bytes are stored through a
pluggable adapter (`STORAGE_BACKEND=local` filesystem or `s3`/S3-compatible) keyed
by file hash, recorded in `blob_objects`, and served only through the separately
keyed `/api/v1/gateway/files/{id}/binary` endpoint. Uploads re-verify SHA-256
before any byte is persisted.

## Alert delivery boundary

Alerts are persisted first; delivery is a separate, retried concern. Enabled
channels (`ALERT_CHANNELS=email,webhook,wecom,dingtalk`) each get one durable
`alert_deliveries` row per alert. Failed sends retry with backoff and become
`dead_letter` after `ALERT_RETRY_MAX`; re-firing the same fingerprint within
`ALERT_SUPPRESS_WINDOW_SECONDS` is suppressed and audited rather than re-sent.

## Identity boundary

Production identity layers legacy bootstrap header keys, database-backed API
keys (rotation/revocation/expiry), signed HS256 JWTs, and an OIDC discovery hook.
MFA and SSO are enforced at the IdP / reverse proxy; the Hub verifies the
resulting identity. All roles remain scoped to specific endpoints.

## Backup boundary

`POST /api/v1/admin/backup` (or the `backup` task kind) produces a timestamped
run with a database dump, a storage archive, and a checksummed manifest.
`scripts/restore.sh` restores it; RPO/RTO targets are defined in the runbook.

Operational alerts are persisted with a stable fingerprint. Node liveness, file reconciliation, task failures, and recovery transitions update alert state idempotently; operators acknowledge or resolve alerts through the admin API and console.

The mobile control plane uses the same center API. Mobile clients queue remote commands and poll command status; they never connect directly to edge nodes. Command execution is deliberately adapter-driven and returns `adapter_pending` until an authenticated Agent/MQTT adapter acknowledges it.

## File parsing boundary

The current content adapter accepts only UTF-8 text over an authenticated HTTP endpoint. The request must identify the source node and repeat the reported file hash; the Hub recomputes SHA-256 before queuing work. Parsing is deterministic: normalize line endings, preserve paragraph boundaries where possible, and split oversized paragraphs at `PARSE_CHUNK_CHARS`. Chunks are immutable records for a file/hash task attempt and carry their own hash. A successful parse moves the file to `parsed`; a failed parse moves it to `parse_failed` and leaves the task retryable after a corrected upload.

This stage deliberately does not claim to read edge disks, control Syncthing, generate embeddings, or call Dify. Those require separate production adapters with their own contracts and acceptance gates.

## File liveness reconciliation

Every file report updates `alive=true` and `last_seen_at`. The Worker invokes the liveness scan on each poll and marks records older than `FILE_MISSING_AFTER_SECONDS` as `alive=false, status=missing`; each transition is audited. A later matching report recovers the record to `alive=true, status=reported` and the normal registration/parse state machine can continue. This is a manifest freshness check, not proof that the Hub can read or hash a remote edge disk.

## Failure semantics

- Offline is derived from missed heartbeats, not inferred from a single network error.
- File reports are idempotent on `(path, file_hash)`.
- Task creation is idempotent on `idempotency_key`.
- Proposal review is a one-way state transition and is audited.
- Missing files require a separate reconciliation job to mark tombstones and schedule replica repair.
- Every file report upserts a `file_replicas` relation. This is reporting evidence, not proof that bytes have been copied. `REPLICA_NODE_IDS` creates idempotent `replica_repair` tasks for missing targets.
- File reports enqueue a deterministic `file_register` task. This stage only validates metadata/hash lineage and marks the record `registered`; parsing, chunking, embeddings, replica synchronization, and Dify retrieval remain separate adapters.
- Content uploads enqueue a deterministic `file_parse` task. The task is idempotent on `(file_id, file_hash)`, replaces chunks only after lineage and content-hash checks pass, and exposes failure state for operator retry.
