# Operations runbook

## Before production

1. Replace development passwords and configure a secret manager.
2. Enable MQTT TLS on 8883, per-node credentials, ACLs, and certificate rotation. Generate the CA and bridge certificate with `scripts/generate-mqtt-certs.sh`; generate each Agent certificate with `scripts/generate-mqtt-node-cert.sh <node-id>`, then provision the same username in EMQX with `EMQX_ADMIN_PASSWORD=<dashboard-password> scripts/provision-mqtt-user.sh <node-id> <strong-password>`. The provisioning script uses the EMQX 5.8 REST API; it does not depend on `emqx ctl users`. Start the secure overlay with `docker compose -f docker-compose.yml -f docker-compose.mqtt-tls.yml up -d --build`. The overlay intentionally fails fast unless `MQTT_PASSWORD` and `MQTT_BRIDGE_API_KEY` are set.
3. Put the API behind HTTPS and SSO with least-privilege RBAC and MFA for administrators.
   The baseline API also supports `ADMIN_API_KEY` and `NODE_API_KEY` as a minimum bootstrap guard; set both in production.
4. Configure PostgreSQL and replica-store backups. Target and document RPO/RTO; perform a restore drill.
5. Set log retention, audit-log export, metrics scraping, and alert routing.
6. Test Windows edge installation, signed binary upgrades, antivirus exclusions, and rollback.
7. Before enabling mobile operations, set `MOBILE_API_KEY`, enforce HTTPS, and connect the mobile identity to OIDC/SSO with RBAC and MFA.
8. Before accepting Dify or frontend feedback/retrieval events, set `FLYWHEEL_INGEST_API_KEY` and configure the gateway to inject the authenticated principal as `X-Actor`; never trust a client-supplied `actor` field.
9. Configure `DOWNLOAD_API_KEY` for the download gateway. Only route Dify/frontend download requests through `GET /api/v1/gateway/files/{file_id}`; do not expose database or edge paths directly. Verify `ETag` and `X-File-Hash` when caching or auditing a download.
10. Configure Dify webhook delivery to `POST /api/v1/integrations/dify/flywheel-events` with `FLYWHEEL_INGEST_API_KEY`; use stable event IDs as `idempotency_key` and verify duplicate delivery returns the original event ID.
11. Task execution defaults to `TASK_EXECUTION_MODE=poll`. To enable Redis/Celery acceleration, set `TASK_EXECUTION_MODE=celery` and start `docker compose --profile celery up -d --build`. Keep the normal `worker` service running: it is the automatic recovery path when publishing or broker delivery fails. Check `/api/v1/executor/health`; `celery_with_poll_fallback` confirms the adapter is loaded, while publish success/failure is persisted in `audit_logs`.

## Routine checks

- Review offline nodes and missing files from the console.
- The Worker runs the file liveness reconciliation on every poll. Confirm `FILE_MISSING_AFTER_SECONDS` matches the Agent reporting interval plus the tolerated outage window; use `POST /api/v1/reconciliation/files` for an immediate operator check.
- Review failed/dead-letter tasks and retry only after checking the error.
- Compare replica counts against policy and run reconciliation after network partitions.
- 固定副本 Agent 必须使用 `--is-replica` 心跳注册，再设置 `REPLICA_NODE_IDS=node-a,node-b`。控制台和 `/api/v1/replica-policy/health` 会拒绝未注册或未启用副本能力的目标。
- `replica_repair` 的 `waiting` 仅表示同步命令已派发；只有 Agent 回传匹配哈希的验证结果才变为 `success`。失败任务可在控制台或 `POST /api/v1/replica-repairs/{id}/retry` 重试。
- Review pending AI proposals; reject anything without a human-verifiable source.
- Check the console alert center or `GET /api/v1/alerts` for node-offline, file-missing, and task-failed events. Acknowledge only after ownership is assigned; resolve only after the underlying condition is verified.
- Review the console `数据飞轮` table or `GET /api/v1/flywheel/gaps` for high-score knowledge gaps. Use `POST /api/v1/flywheel/proposals?query=...` only to create a draft; every draft must remain pending until a named administrator reviews it through `POST /api/v1/proposals/{id}/review`.
- Verify `POST /api/v1/knowledge/search` after parsing. `ollama/ready` means the embedding service answered; `deterministic_fallback/degraded` is explicit offline fallback.
- Keep flywheel event idempotency keys stable per Dify turn/retrieval. Do not accept client-supplied actor values as identity in production; inject identity at the gateway and retain the original source in metadata for audit.

## MQTT security acceptance

- `1883` is disabled by `docker-compose.mqtt-tls.yml`; only `8883` is published.
- The Agent refuses plaintext broker URLs and requires a trusted CA, per-node password, and username equal to its stable node ID. Optional client certificates can be enabled together with a private key, but the default overlay authenticates MQTT application identity with EMQX's per-node password database; TLS always authenticates the broker.
- The Agent publishes retained `knowledge/nodes/<node-id>/status` messages and sets an offline last-will. The bridge validates the topic/payload node ID match before forwarding through `/internal/v1/mqtt/node-status` with a separate bridge key.
- The bridge never consumes arbitrary topics or writes the database directly. Rotate node passwords, client certificates, and `MQTT_BRIDGE_API_KEY` as part of the normal secret-rotation runbook.

## Production adapters runbook

### LangGraph orchestration

- Default `ORCHESTRATION_MODE=state_machine` is always valid. To use the real graph: `pip install -r requirements-langgraph.txt`, set `ORCHESTRATION_MODE=langgraph`, and rebuild the image.
- Verify `GET /api/v1/orchestration/status`; `effective_mode` must be `langgraph` before claiming graph execution.
- Enqueue a whole-file run with `POST /api/v1/pipeline/run` (admin). The older per-stage tasks keep working for resumable/partial runs.

### DeepSeek-Harness review

- Set `DSH_ENABLED=true`, `DSH_BASE_URL`, `DSH_API_KEY`, and an explicit `DSH_ALLOWED_HOSTS` allowlist.
- Trigger `POST /api/v1/dsh/review?file_id=<id>` only for already parsed files. The output is a pending `document_review` Proposal; it can never mutate knowledge data.
- When DSH is down the adapter returns `degraded` with a deterministic fallback, clearly labelled in the Proposal body.

### Binary object storage

- Upload via `POST /api/v1/files/{id}/blob` with `X-File-Hash`, `X-Node-Id`, and the raw bytes; the Hub re-checks SHA-256.
- Serve via `GET /api/v1/gateway/files/{id}/binary` with `X-Download-Key`. Never expose storage paths directly.
- For production object storage set `STORAGE_BACKEND=s3` plus `S3_BUCKET`, `S3_ENDPOINT_URL`, and credentials; `local` is for single-host only.

### External alert delivery

- Set `ALERT_CHANNELS` to a comma list of `email`, `webhook`, `wecom`, `dingtalk` and fill the matching endpoint/credentials.
- Review `GET /api/v1/alert-deliveries`; failed rows retry automatically from the worker, and exhausted rows land in `dead_letter`. Force a delivery cycle with `POST /api/v1/alert-deliveries/process`.
- Suppression is per alert fingerprint within `ALERT_SUPPRESS_WINDOW_SECONDS`; suppressed sends are audited.

### Identity and RBAC

- Bootstrap with `scripts/init-production-env.sh` to generate `ADMIN_API_KEY`, `NODE_API_KEY`, `SECURITY_SECRET`, and the other scoped keys.
- Rotate service keys with `POST /api/v1/admin/keys` (plaintext shown once) and revoke with `POST /api/v1/admin/keys/{key_id}/revoke`. The legacy header keys remain valid until removed from `.env`.
- For SSO/MFA, put an IdP/authenticating reverse proxy in front and set `OIDC_DISCOVERY_URL`/`OIDC_ISSUER`/`OIDC_CLIENT_ID`; verify identity at the gateway and pass a scoped JWT or API key to the Hub.

### Backup and restore

- One-click backup: `POST /api/v1/admin/backup` (or `scripts/backup.sh`). Inspect `GET /api/v1/admin/backup/status`.
- Restore with `scripts/restore.sh <backup_id>`; it is destructive and stops services while restoring.
- Documented targets: **RPO ≤ 24 h** (scheduled backup), **RTO ≤ 4 h** (restore drill on identical hardware). Run a restore drill before onboarding business data.

### Dify

- Deploy stock Dify from `knowledge-hub/dify/` (or the upstream compose). Wire the three Hub endpoints documented in `knowledge-hub/dify/README.md`. Never patch Dify source.

### Windows Agent service

- Build with `scripts/build-all.sh` (or `knowledge-edge-agent/scripts/windows/build-windows.bat`), sign with a trusted certificate, and install the native service with `install-service.bat` (or `-service install`). See `knowledge-edge-agent/BUILD-WINDOWS.md`.

## Host acceptance check

From the repository root, with the Compose stack healthy, run `make acceptance-smoke`.
The command creates a temporary node and test records, then checks:

- `/healthz` returns `ok`.
- A heartbeat registers an online node.
- Repeating the same file report remains one metadata record.
- The file registration task advances the reported file to `registered` while preserving its hash lineage.
- A matching UTF-8 content upload advances the same file to `parsed`, creates at least one deterministic chunk, and exposes the successful parse task.
- A liveness reconciliation finds the just-reported file alive and leaves it out of the missing count.
- The worker completes a deterministic `noop` task.
- An unsupported task enters `failed` with an explicit executor error.
- A reserved mobile command moves `queued -> running -> success` through the edge claim/ACK contract.
- A replica repair moves `pending -> running -> waiting`, dispatches `sync_replica`, and only completes after a verified hash ACK records the healthy replica.

The script is safe to rerun because it uses unique idempotency keys. Its replica ACK is a controlled center-contract simulation; it does not exercise real file deletion, actual edge-disk reads, Syncthing byte transfer, MQTT TLS, Dify, vector generation, or AI execution. Those remain separate production acceptance gates.

## Flywheel acceptance check

1. POST one retrieval event with `result_count: 0` and repeat the same idempotency key; the second request must return the same event ID.
2. POST a low-rating feedback event for the same normalized query.
3. GET `/api/v1/flywheel/gaps`; verify one grouped row with deterministic score `2 * no_result_count + negative_feedback_count`.
4. Create a flywheel Proposal from that query and verify its body contains `human_review_required: true`.
5. Confirm the Proposal remains pending until the administrator explicitly approves or rejects it. The mobile endpoint `/api/v1/mobile/flywheel/gaps` is read-only and must not create or review proposals.

## Acceptance gates

The release is not production-ready until duplicate events, worker crashes, node loss, file deletion, network partitions, model outage, backup restore, and unauthorized approval attempts pass automated tests.
