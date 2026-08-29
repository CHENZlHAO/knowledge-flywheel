# Dify integration (optional, separate deployment)

Dify is the user-facing knowledge QA/preview/download frontend. Per the system
architecture it stays **unmodified stock Dify** — the Hub never patches its
source, so it can follow upstream upgrades.

## Deploy Dify

Either use the pinned, self-contained stack here:

```bash
cd knowledge-hub/dify
cp .env.example .env
docker compose up -d
```

or clone the official [langgenius/dify](https://github.com/langgenius/dify)
repository and run its authoritative `docker/docker-compose.yaml`. Set
`DIFY_VERSION` to the upstream release you validated (the Hub was verified
against `1.12.x` line; newer patch releases are expected to be drop-in).

> The `langgenius/dify-*` images may not exist on every domestic mirror. If the
> pull fails, set `DIFY_IMAGE_PREFIX=docker.io/langgenius` and use a registry
> proxy, or import the images into your private registry.

## Three Hub integration points (no Dify source changes)

| Purpose | Hub endpoint | Notes |
|---|---|---|
| Knowledge retrieval log + feedback webhook | `POST http://<hub>:8000/api/v1/integrations/dify/flywheel-events` | Header `X-Flywheel-Key: $FLYWHEEL_INGEST_API_KEY` and `X-Actor: <authenticated principal>`. Stable per-turn id as `idempotency_key`. |
| Download gateway | `GET http://<hub>:8000/api/v1/gateway/files/{file_id}` or `/binary` | Header `X-Download-Key: $DOWNLOAD_API_KEY`. Never expose DB or edge paths to Dify. |
| Knowledge search | `POST http://<hub>:8000/api/v1/knowledge/search` | Header `X-Search-Key: $SEARCH_API_KEY`. Body `{"query": "...", "top_k": 5}`. |

Configure these URLs in Dify as a custom tool / HTTP request node or in your
Dify knowledge-retrieval callback. Keep event IDs stable per Dify turn so the
Hub's idempotency dedup returns the same event on retries.

## Acceptance

1. Deploy Dify, create an app, and bind a knowledge base.
2. Point its retrieval callback at the Hub webhook and verify `GET /api/v1/flywheel/gaps` accumulates real Dify turns.
3. Route a document preview through the download gateway and verify `ETag`/`X-File-Hash` match the Hub file record.
4. Upgrade Dify using its own documented procedure; the Hub must require zero changes.
