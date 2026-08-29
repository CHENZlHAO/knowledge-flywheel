import os
import hashlib
from datetime import datetime, timedelta, timezone
from uuid import uuid4
os.environ["DATABASE_URL"] = "sqlite:///./test.db"
from fastapi.testclient import TestClient
from app.main import app
from app.db import SessionLocal
from app.models import Alert, DocumentChunk, FileRecord, FlywheelEvent, Node, RemoteCommand, Task
from app.services import run_task_once


def test_health_and_idempotent_reports():
    with TestClient(app) as client:
        assert client.get("/healthz").json()["status"] == "ok"
        heartbeat = {"node_id":"n1","hostname":"pc-1","cpu_percent":12.5,"disk_free_bytes":1000}
        assert client.post("/api/v1/nodes/heartbeat", json=heartbeat).status_code == 200
        report = {"node_id":"n1","path":"docs/a.md","file_hash":"0123456789abcdef","size_bytes":10}
        assert client.post("/api/v1/files/report", json=report).status_code == 200
        assert client.post("/api/v1/files/report", json=report).status_code == 200
        assert client.get("/api/v1/files/summary").json()["total"] == 1
        pipeline = client.get("/api/v1/pipeline/files").json()
        assert pipeline[0]["task_status"] == "pending"


def test_mqtt_bridge_status_endpoint_requires_dedicated_key(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "mqtt_bridge_api_key", "bridge-secret")
    payload = {
        "node_id": "mqtt-node",
        "hostname": "mqtt-pc",
        "agent_version": "0.2.0",
        "cpu_percent": 1,
        "disk_free_bytes": 100,
        "is_replica": False,
        "status": "offline",
        "reason": "mqtt_will",
    }
    with TestClient(app) as client:
        assert client.post("/internal/v1/mqtt/node-status", json=payload).status_code == 401
        response = client.post(
            "/internal/v1/mqtt/node-status",
            json=payload,
            headers={"X-Bridge-Key": "bridge-secret"},
        )
        assert response.status_code == 200
        assert response.json()["status"] == "offline"


def test_task_retry_requires_failed_state():
    with TestClient(app) as client:
        payload = {"kind":"parse","idempotency_key":"test-task-1","payload":{"file_id":1}}
        task = client.post("/api/v1/tasks", json=payload).json()
        assert client.post(f"/api/v1/tasks/{task['id']}/retry").status_code == 409


def test_mobile_command_is_queued_and_idempotent():
    with TestClient(app) as client:
        heartbeat = {"node_id":"mobile-node","hostname":"mobile-pc","cpu_percent":5,"disk_free_bytes":2000}
        assert client.post("/api/v1/nodes/heartbeat", json=heartbeat).status_code == 200
        command = {
            "node_id":"mobile-node",
            "command_type":"restart_agent",
            "idempotency_key":f"mobile-command-{uuid4()}",
            "payload":{},
            "requested_by":"mobile-user",
        }
        first = client.post("/api/v1/mobile/commands", json=command)
        second = client.post("/api/v1/mobile/commands", json=command)
        assert first.status_code == 200
        assert first.json()["status"] == "queued"
        assert first.json()["execution_mode"] == "adapter_pending"
        assert second.json()["id"] == first.json()["id"]
        status = client.get(f"/api/v1/mobile/commands/{first.json()['id']}")
        assert status.json()["status"] == "queued"


def test_edge_can_claim_and_ack_mobile_command():
    with TestClient(app) as client:
        heartbeat = {"node_id":"command-node","hostname":"command-pc","cpu_percent":5,"disk_free_bytes":2000}
        client.post("/api/v1/nodes/heartbeat", json=heartbeat)
        command = {
            "node_id":"command-node",
            "command_type":"reset_sync",
            "idempotency_key":f"mobile-command-claim-{uuid4()}",
            "payload":{"folder":"docs"},
            "requested_by":"mobile-user",
        }
        created = client.post("/api/v1/mobile/commands", json=command).json()
        claimed = client.get("/api/v1/nodes/command-node/commands/next").json()["command"]
        assert claimed["id"] == created["id"]
        assert claimed["status"] == "running"
        assert client.get("/api/v1/nodes/command-node/commands/next").json()["command"] is None
        ack = {"status":"success","result":{"adapter":"test"}}
        assert client.post(f"/api/v1/nodes/command-node/commands/{created['id']}/ack", json=ack).status_code == 200
        assert client.get(f"/api/v1/mobile/commands/{created['id']}").json()["status"] == "success"


def test_expired_command_lease_is_requeued():
    with TestClient(app) as client:
        node_id = f"lease-node-{uuid4()}"
        heartbeat = {"node_id":node_id,"hostname":"lease-pc","cpu_percent":5,"disk_free_bytes":2000}
        client.post("/api/v1/nodes/heartbeat", json=heartbeat)
        command = {
            "node_id":node_id,
            "command_type":"retry_task",
            "idempotency_key":f"mobile-command-lease-{uuid4()}",
            "payload":{},
            "requested_by":"mobile-user",
        }
        created = client.post("/api/v1/mobile/commands", json=command).json()
        claimed = client.get(f"/api/v1/nodes/{node_id}/commands/next").json()["command"]
        assert claimed["status"] == "running"
        with SessionLocal() as db:
            row = db.get(RemoteCommand, created["id"])
            row.claimed_at = datetime.now(timezone.utc) - timedelta(minutes=10)
            db.commit()
        reclaimed = client.get(f"/api/v1/nodes/{node_id}/commands/next").json()["command"]
        assert reclaimed["id"] == created["id"]
        assert reclaimed["status"] == "running"


def test_file_content_upload_parses_and_lists_chunks():
    with TestClient(app) as client:
        node_id = f"parse-api-{uuid4()}"
        content = "标题\n\n正文内容"
        file_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        heartbeat = {"node_id": node_id, "hostname": "parse-api-pc", "cpu_percent": 5, "disk_free_bytes": 2000}
        assert client.post("/api/v1/nodes/heartbeat", json=heartbeat).status_code == 200
        report = client.post(
            "/api/v1/files/report",
            json={"node_id": node_id, "path": f"docs/{node_id}.md", "file_hash": file_hash, "size_bytes": len(content.encode())},
        )
        file_id = report.json()["id"]
        queued = client.post(
            f"/api/v1/files/{file_id}/content",
            json={"source_node_id": node_id, "file_hash": file_hash, "content": content},
        )
        assert queued.status_code == 200
        for _ in range(10):
            with SessionLocal() as db:
                target = db.get(Task, queued.json()["task_id"])
                if target and target.status == "success":
                    break
                run_task_once(db, "api-parse-worker", lease_seconds=60)
        chunks = client.get(f"/api/v1/files/{file_id}/chunks")
        assert chunks.status_code == 200
        assert [chunk["content"] for chunk in chunks.json()] == [content]
        pipeline = next(item for item in client.get("/api/v1/pipeline/files").json() if item["id"] == file_id)
        assert pipeline["status"] == "parsed"
        assert pipeline["chunk_count"] == 1
        assert pipeline["parse_task_status"] == "success"


def test_file_content_upload_rejects_hash_mismatch():
    with TestClient(app) as client:
        node_id = f"parse-api-bad-{uuid4()}"
        client.post("/api/v1/nodes/heartbeat", json={"node_id": node_id, "hostname": "parse-bad-pc", "cpu_percent": 1, "disk_free_bytes": 2000})
        file_hash = "c" * 64
        file_id = client.post(
            "/api/v1/files/report",
            json={"node_id": node_id, "path": f"docs/{node_id}.txt", "file_hash": file_hash, "size_bytes": 3},
        ).json()["id"]
        response = client.post(
            f"/api/v1/files/{file_id}/content",
            json={"source_node_id": node_id, "file_hash": file_hash, "content": "bad"},
        )
        assert response.status_code == 409
        assert "content hash" in response.json()["detail"]


def test_file_reconciliation_endpoint_marks_stale_file_missing():
    with TestClient(app) as client:
        node_id = f"reconcile-{uuid4()}"
        client.post("/api/v1/nodes/heartbeat", json={"node_id": node_id, "hostname": "reconcile-pc", "cpu_percent": 1, "disk_free_bytes": 2000})
        file_id = client.post(
            "/api/v1/files/report",
            json={"node_id": node_id, "path": f"docs/{node_id}.txt", "file_hash": "e" * 64, "size_bytes": 3},
        ).json()["id"]
        with SessionLocal() as db:
            row = db.get(FileRecord, file_id)
            row.last_seen_at = datetime.now(timezone.utc) - timedelta(hours=1)
            db.commit()
        result = client.post("/api/v1/reconciliation/files")
        assert result.status_code == 200
        assert result.json()["marked_missing"] == 1
        pipeline = next(item for item in client.get("/api/v1/pipeline/files").json() if item["id"] == file_id)
        assert pipeline["alive"] is False
        assert pipeline["status"] == "missing"


def test_file_replica_is_visible_by_file_and_node():
    with TestClient(app) as client:
        node_id = f"replica-{uuid4()}"
        client.post("/api/v1/nodes/heartbeat", json={"node_id": node_id, "hostname": "replica-pc", "cpu_percent": 1, "disk_free_bytes": 2000})
        file_id = client.post(
            "/api/v1/files/report",
            json={"node_id": node_id, "path": f"docs/{node_id}.txt", "file_hash": "f" * 64, "size_bytes": 3},
        ).json()["id"]
        replicas = client.get(f"/api/v1/files/{file_id}/replicas")
        assert replicas.status_code == 200
        assert replicas.json()[0]["node_id"] == node_id
        node_files = client.get(f"/api/v1/nodes/{node_id}/files")
        assert node_files.status_code == 200
        assert node_files.json()[0]["file_id"] == file_id


def test_replica_policy_health_exposes_invalid_targets_and_repair_status():
    with TestClient(app) as client:
        with SessionLocal() as db:
            db.add(Node(id="policy-valid", hostname="policy-valid", is_replica=True))
            db.add(Node(id="policy-invalid", hostname="policy-invalid", is_replica=False))
            db.commit()

        response = client.get("/api/v1/replica-policy/health?node_ids=policy-valid,policy-invalid,unknown")

        assert response.status_code == 200
        assert response.json()["valid_nodes"] == ["policy-valid"]
        assert response.json()["invalid_nodes"] == ["policy-invalid", "unknown"]


def test_failed_replica_repair_has_dedicated_retry_endpoint():
    with TestClient(app) as client:
        with SessionLocal() as db:
            task = Task(
                idempotency_key=f"repair-retry-{uuid4()}",
                kind="replica_repair",
                status="failed",
                payload_json="{}",
                error="adapter missing",
            )
            db.add(task)
            db.commit()
            task_id = task.id

        response = client.post(f"/api/v1/replica-repairs/{task_id}/retry")

        assert response.status_code == 200
        assert response.json()["status"] == "pending"


def test_node_offline_alert_is_created_and_resolved_by_heartbeat(monkeypatch):
    with TestClient(app) as client:
        from app.config import settings
        monkeypatch.setattr(settings, "mqtt_bridge_api_key", "bridge-alert-secret")
        node_id = f"alert-node-{uuid4()}"
        payload = {"node_id": node_id, "hostname": "alert-pc", "status": "offline", "reason": "mqtt_will", "cpu_percent": 0, "disk_free_bytes": 0}
        response = client.post("/internal/v1/mqtt/node-status", json=payload, headers={"X-Bridge-Key": "bridge-alert-secret"})
        assert response.status_code == 200
        alerts = client.get("/api/v1/alerts?status=open").json()
        alert = next(item for item in alerts if item["kind"] == "node_offline" and item["resource_id"] == node_id)
        assert client.post("/api/v1/alerts/%s/ack" % alert["id"]).json()["status"] == "acknowledged"
        client.post("/api/v1/nodes/heartbeat", json={"node_id": node_id, "hostname": "alert-pc", "cpu_percent": 1, "disk_free_bytes": 100})
        resolved = client.get("/api/v1/alerts").json()
        assert next(item for item in resolved if item["id"] == alert["id"])["status"] == "resolved"


def test_missing_file_alert_is_created_and_recovered():
    with TestClient(app) as client:
        node_id = f"file-alert-{uuid4()}"
        client.post("/api/v1/nodes/heartbeat", json={"node_id": node_id, "hostname": "file-alert-pc", "cpu_percent": 1, "disk_free_bytes": 100})
        file_id = client.post("/api/v1/files/report", json={"node_id": node_id, "path": f"docs/{node_id}.txt", "file_hash": "a" * 64, "size_bytes": 1}).json()["id"]
        with SessionLocal() as db:
            row = db.get(FileRecord, file_id)
            row.last_seen_at = datetime.now(timezone.utc) - timedelta(hours=1)
            db.commit()
        client.post("/api/v1/reconciliation/files")
        alert = next(item for item in client.get("/api/v1/alerts?status=open").json() if item["resource_id"] == str(file_id))
        client.post("/api/v1/files/report", json={"node_id": node_id, "path": f"docs/{node_id}.txt", "file_hash": "a" * 64, "size_bytes": 1})
        assert next(item for item in client.get("/api/v1/alerts").json() if item["id"] == alert["id"])["status"] == "resolved"


def test_failed_task_alert_can_be_acknowledged_and_resolved():
    with TestClient(app) as client:
        task = client.post("/api/v1/tasks", json={"kind": "unsupported", "idempotency_key": f"alert-task-{uuid4()}", "payload": {}}).json()
        with SessionLocal() as db:
            from app.services import run_task_once
            run_task_once(db, "alert-worker")
        alert = next(item for item in client.get("/api/v1/alerts?status=open").json() if item["resource_id"] == str(task["id"]))
        assert client.post(f"/api/v1/alerts/{alert['id']}/ack").json()["status"] == "acknowledged"
        assert client.post(f"/api/v1/alerts/{alert['id']}/resolve").json()["status"] == "resolved"


def test_flywheel_events_are_idempotent_and_aggregate_into_proposal():
    with TestClient(app) as client:
        query = "如何 申请 年假"
        retrieval = {"idempotency_key": "retrieval-gap-1", "query": query, "result_count": 0}
        first = client.post("/api/v1/flywheel/retrievals", json=retrieval)
        second = client.post("/api/v1/flywheel/retrievals", json=retrieval)
        assert first.status_code == 200 and second.json()["id"] == first.json()["id"]
        feedback = client.post("/api/v1/flywheel/feedback", json={"idempotency_key": "feedback-gap-1", "query": "  如何 申请 年假 ", "rating": 1})
        assert feedback.status_code == 200
        gaps = client.get("/api/v1/flywheel/gaps").json()
        gap = next(item for item in gaps if item["normalized_query"] == "如何申请年假")
        assert gap["no_result_count"] == 1 and gap["negative_feedback_count"] == 1 and gap["score"] == 3
        proposal = client.post("/api/v1/flywheel/proposals", params={"query": query})
        assert proposal.status_code == 200
        assert proposal.json()["kind"] == "flywheel_optimization"
        assert "human_review_required" in proposal.json()["body"]
        duplicate = client.post("/api/v1/flywheel/proposals", params={"query": query})
        assert duplicate.json()["id"] == proposal.json()["id"]
        reviewed = client.post(f"/api/v1/proposals/{proposal.json()['id']}/review", json={"decision": "approved", "reviewer": "qa-admin"})
        assert reviewed.status_code == 200 and reviewed.json()["status"] == "approved"
        assert client.post(f"/api/v1/proposals/{proposal.json()['id']}/review", json={"decision": "rejected", "reviewer": "qa-admin"}).status_code == 409


def test_mobile_flywheel_gaps_are_read_only():
    with TestClient(app) as client:
        client.post("/api/v1/flywheel/retrievals", json={"idempotency_key": "mobile-gap-1", "query": "手机只读缺口", "result_count": 0})
        response = client.get("/api/v1/mobile/flywheel/gaps")
        assert response.status_code == 200
        assert response.json()[0]["normalized_query"] == "手机只读缺口"


def test_search_returns_versioned_embedding_hits_and_records_retrieval(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "embedding_provider", "deterministic")
    with TestClient(app) as client:
        node_id = f"search-{uuid4()}"
        client.post("/api/v1/nodes/heartbeat", json={"node_id": node_id, "hostname": node_id, "cpu_percent": 1, "disk_free_bytes": 100})
        content = "企业报销流程：提交发票后由直属主管审批。"
        file_hash = hashlib.sha256(content.encode()).hexdigest()
        file_id = client.post("/api/v1/files/report", json={"node_id": node_id, "path": f"docs/{node_id}.md", "file_hash": file_hash, "size_bytes": len(content.encode())}).json()["id"]
        client.post(f"/api/v1/files/{file_id}/content", json={"source_node_id": node_id, "file_hash": file_hash, "content": content})
        with SessionLocal() as db:
            run_task_once(db, "search-worker")
            run_task_once(db, "search-worker")
            run_task_once(db, "search-worker")
        response = client.post("/api/v1/knowledge/search", json={"query": "报销流程", "top_k": 5, "idempotency_key": f"search-event-{uuid4()}"})
        assert response.status_code == 200
        body = response.json()
        assert body["count"] == 1
        assert body["results"][0]["file_hash"] == file_hash
        assert body["results"][0]["embedding_status"] == "degraded"
        assert body["embedding_provider"] == "deterministic"
        assert body["retrieval_event_id"] > 0


def test_search_empty_result_is_recorded_and_replay_is_idempotent(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "embedding_provider", "deterministic")
    key = f"search-empty-{uuid4()}"
    with TestClient(app) as client:
        payload = {"query": "不存在的制度", "top_k": 3, "idempotency_key": key}
        first = client.post("/api/v1/knowledge/search", json=payload)
        second = client.post("/api/v1/knowledge/search", json=payload)
        assert first.status_code == second.status_code == 200
        assert first.json()["count"] == 0
        assert first.json()["retrieval_event_id"] == second.json()["retrieval_event_id"]
        gaps = client.get("/api/v1/flywheel/gaps").json()
        assert next(item for item in gaps if item["normalized_query"] == "不存在的制度")["no_result_count"] == 1


def test_production_search_requires_gateway_key(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "app_env", "production")
    monkeypatch.setattr(settings, "search_api_key", "search-secret")
    monkeypatch.setattr(settings, "embedding_provider", "deterministic")
    with TestClient(app) as client:
        assert client.post("/api/v1/knowledge/search", json={"query": "x"}).status_code == 401
        assert client.post("/api/v1/knowledge/search", json={"query": "x"}, headers={"X-Search-Key": "search-secret"}).status_code == 200


def test_production_flywheel_ingest_requires_key_and_gateway_actor(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "app_env", "production")
    monkeypatch.setattr(settings, "flywheel_ingest_api_key", "flywheel-secret")
    payload = {
        "idempotency_key": f"prod-flywheel-{uuid4()}",
        "query": "生产可信身份",
        "result_count": 0,
        "actor": "forged-client-actor",
        "metadata": {"source": "test"},
    }
    with TestClient(app) as client:
        assert client.post("/api/v1/flywheel/retrievals", json=payload).status_code == 401
        response = client.post(
            "/api/v1/flywheel/retrievals",
            json=payload,
            headers={"X-Flywheel-Key": "flywheel-secret", "X-Actor": "trusted-user-42"},
        )
        assert response.status_code == 200
        with SessionLocal() as db:
            event = db.get(FlywheelEvent, response.json()["id"])
            assert event.actor == "trusted-user-42"
            assert "client_actor" in event.metadata_json


def test_search_excludes_superseded_file_hash(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "embedding_provider", "deterministic")
    with TestClient(app) as client:
        node_id = f"version-search-{uuid4()}"
        path = f"docs/{node_id}.md"
        client.post("/api/v1/nodes/heartbeat", json={"node_id": node_id, "hostname": node_id, "cpu_percent": 1, "disk_free_bytes": 100})
        file_ids = []
        for content in ("旧版差旅制度", "新版差旅制度"):
            file_hash = hashlib.sha256(content.encode()).hexdigest()
            file_id = client.post("/api/v1/files/report", json={"node_id": node_id, "path": path, "file_hash": file_hash, "size_bytes": len(content.encode())}).json()["id"]
            client.post(f"/api/v1/files/{file_id}/content", json={"source_node_id": node_id, "file_hash": file_hash, "content": content})
            with SessionLocal() as db:
                run_task_once(db, "version-worker")
                run_task_once(db, "version-worker")
                run_task_once(db, "version-worker")
            file_ids.append(file_id)
        response = client.post("/api/v1/knowledge/search", json={"query": "差旅制度", "top_k": 10}).json()
        assert {item["file_id"] for item in response["results"]} == {file_ids[1]}
        pipeline = {item["id"]: item for item in client.get("/api/v1/pipeline/files").json()}
        assert pipeline[file_ids[0]]["status"] == "superseded"
        assert pipeline[file_ids[0]]["alive"] is False
        assert pipeline[file_ids[1]]["version"] == 2


def test_download_gateway_requires_key_and_serves_only_current_version(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "download_api_key", "download-secret")
    with TestClient(app) as client:
        node_id = f"download-{uuid4()}"
        client.post("/api/v1/nodes/heartbeat", json={"node_id": node_id, "hostname": node_id, "cpu_percent": 1, "disk_free_bytes": 100})
        content = "网关预览内容"
        file_hash = hashlib.sha256(content.encode()).hexdigest()
        file_id = client.post("/api/v1/files/report", json={"node_id": node_id, "path": f"docs/{node_id}.md", "file_hash": file_hash, "size_bytes": len(content.encode())}).json()["id"]
        client.post(f"/api/v1/files/{file_id}/content", json={"source_node_id": node_id, "file_hash": file_hash, "content": content})
        with SessionLocal() as db:
            run_task_once(db, "download-worker")
            run_task_once(db, "download-worker")
            run_task_once(db, "download-worker")
        assert client.get(f"/api/v1/gateway/files/{file_id}").status_code == 200
        response = client.get(f"/api/v1/gateway/files/{file_id}", headers={"X-Download-Key": "download-secret"})
        assert response.status_code == 200
        assert response.text == content
        assert response.headers["etag"] == f'"{file_hash}"'
        assert response.headers["x-file-version"] == "1"

        with SessionLocal() as db:
            row = db.get(FileRecord, file_id)
            row.alive = False
            row.status = "superseded"
            db.commit()
        assert client.get(f"/api/v1/gateway/files/{file_id}", headers={"X-Download-Key": "download-secret"}).status_code == 404


def test_dify_webhook_normalizes_events_and_is_idempotent(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "app_env", "production")
    monkeypatch.setattr(settings, "flywheel_ingest_api_key", "dify-secret")
    key = f"dify-event-{uuid4()}"
    payload = {"event_type": "retrieval", "idempotency_key": key, "query": "Dify webhook", "result_count": 0, "metadata": {"conversation_id": "c-1"}}
    with TestClient(app) as client:
        assert client.post("/api/v1/integrations/dify/flywheel-events", json=payload).status_code == 401
        headers = {"X-Flywheel-Key": "dify-secret", "X-Actor": "dify-user-7"}
        first = client.post("/api/v1/integrations/dify/flywheel-events", json=payload, headers=headers)
        second = client.post("/api/v1/integrations/dify/flywheel-events", json=payload, headers=headers)
        assert first.status_code == second.status_code == 200
        assert first.json()["id"] == second.json()["id"]
        assert first.json()["actor"] == "dify-user-7"
        bad = client.post("/api/v1/integrations/dify/flywheel-events", json={**payload, "idempotency_key": f"bad-{uuid4()}", "result_count": None}, headers=headers)
        assert bad.status_code == 400
