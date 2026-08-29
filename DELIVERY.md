# Delivery manifest — enterprise knowledge flywheel production adapters

This manifest documents the second delivery slice: the production adapters that
close the remaining gaps between the MVP baseline and the target architecture.

## Scope delivered (code + docs + tests)

| Area | Deliverable |
|---|---|
| LangGraph orchestration | `knowledge-hub/app/langgraph_flow.py` — deterministic `register→parse→embed→replica` StateGraph with an import-safe sequential fallback; `ORCHESTRATION_MODE` config; `pipeline_run` task kind; `POST /api/v1/pipeline/run` |
| DeepSeek-Harness adapter | `knowledge-hub/app/dsh_client.py` — review/refine/flywheel-analysis as pending Proposals only, deterministic fallback; `POST /api/v1/dsh/review` |
| Binary object storage | `knowledge-hub/app/storage.py` — local filesystem + S3/MinIO backend; `BlobObject` model; `POST /api/v1/files/{id}/blob`; `GET /api/v1/gateway/files/{id}/binary` |
| External alert delivery | `knowledge-hub/app/alerting.py` — email/webhook/WeCom/DingTalk with retry, backoff, dead-letter, suppression; `AlertDelivery` model |
| RBAC / identity | `knowledge-hub/app/security.py` — roles, HS256 JWT, DB-backed API-key rotation/revocation, OIDC hook; `ApiKey` model; `/api/v1/admin/keys` and `/tokens` |
| Backup / restore | `knowledge-hub/app/backup.py`, `scripts/backup.sh`, `scripts/restore.sh` — database dump + storage archive + checksummed manifest |
| Dify deployment | `knowledge-hub/dify/` — self-contained stock Dify compose + integration README |
| Windows Agent service | `knowledge-edge-agent/service_windows.go` + `service_other.go` + `scripts/windows/*.bat` — native SCM service, signing, staged auto-update contract |
| Delivery packaging | `scripts/init-production-env.sh`, `deploy-center.sh`, `build-all.sh`, `deploy-edge-batch.sh`, `inspect-production.sh`; `Makefile` targets |
| Operator console | `knowledge-hub/app/console.html` — new panels for orchestration/security, alert delivery, binary storage, API keys, backup |
| Docs | `knowledge-hub/docs/{architecture,operations,input-output}.md`, `README.md` |

## Tests written

- `knowledge-hub/tests/test_langgraph_flow.py`
- `knowledge-hub/tests/test_dsh_client.py`
- `knowledge-hub/tests/test_blob_storage.py`
- `knowledge-hub/tests/test_alerting.py`
- `knowledge-hub/tests/test_security.py`
- `knowledge-hub/tests/test_backup.py`
- `knowledge-edge-agent/main_test.go` (one added case)

## Verification status

- ✅ `pytest`: **77 passed** (run from /tmp with `PYTHONPATH=knowledge-hub`, no repo pollution).
- ✅ `go test ./...` (macOS): **15/15 passed**.
- ✅ Windows cross-build: `CGO_ENABLED=0 GOOS=windows GOARCH=amd64 go build` **succeeded** (≈10 MiB EXE).
- ✅ `docker compose config`: validated cleanly.
- ✅ Independent adversarial review performed; findings A1/A2/B1 fixed and re-verified.
- ✅ `go.sum` now contains the `golang.org/x/sys` entries (added by `go mod tidy`).

A live browser/headless smoke of the operator console was not part of the
executed run; the console is a static HTML asset served by the Hub at `/`.

### Still requires external infrastructure/credentials to verify end-to-end

Real Syncthing byte transfer, a running Dify instance, an OIDC IdP/MFA, real
SMTP/WeCom/DingTalk credentials, Ollama models, and a Windows code-signing
certificate. Each is delivered as a real adapter/config with explicit degraded
behavior; none is silently faked.
