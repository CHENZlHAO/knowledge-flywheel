import hashlib
import os
from uuid import uuid4

os.environ["DATABASE_URL"] = "sqlite:///./test.db"

from fastapi.testclient import TestClient

from app import dsh_client
from app.config import settings
from app.db import SessionLocal
from app.main import app
from app.services import create_dsh_review_proposal


def test_dsh_status_reports_disabled_by_default():
    status = dsh_client.status()
    assert status["enabled"] is False
    assert status["base_url"] in (None, "")


def test_review_document_degrades_when_disabled():
    result = dsh_client.review_document("policy.md", "企业报销流程")
    assert result.status in {"disabled", "degraded"}
    assert result.kind == "document_review"


def test_review_document_falls_back_to_deterministic_when_unreachable(monkeypatch):
    monkeypatch.setattr(settings, "dsh_enabled", True)
    monkeypatch.setattr(settings, "dsh_base_url", "http://127.0.0.1:9")
    monkeypatch.setattr(settings, "dsh_allowed_hosts", "127.0.0.1")
    result = dsh_client.review_document("secrets.md", "api_key=abc123")
    assert result.provider == "deterministic_review_fallback"
    assert result.status == "degraded"
    assert result.verdict == "flag"


def test_dsh_review_endpoint_creates_pending_proposal(monkeypatch):
    monkeypatch.setattr(settings, "embedding_provider", "deterministic")
    node_id = f"dsh-node-{uuid4()}"
    content = "报销流程：提交发票后由直属主管审批。"
    file_hash = hashlib.sha256(content.encode()).hexdigest()
    with TestClient(app) as client:
        client.post("/api/v1/nodes/heartbeat", json={"node_id": node_id, "hostname": node_id, "cpu_percent": 1, "disk_free_bytes": 100})
        file_id = client.post(
            "/api/v1/files/report",
            json={"node_id": node_id, "path": f"docs/{node_id}.md", "file_hash": file_hash, "size_bytes": len(content.encode())},
        ).json()["id"]
        client.post(f"/api/v1/files/{file_id}/content", json={"source_node_id": node_id, "file_hash": file_hash, "content": content})
        with SessionLocal() as db:
            from app.services import run_task_once

            run_task_once(db, "dsh-parse-worker")
            run_task_once(db, "dsh-parse-worker")
        response = client.post(f"/api/v1/dsh/review?file_id={file_id}")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "pending"
        assert body["kind"] == "document_review"
        duplicate = client.post(f"/api/v1/dsh/review?file_id={file_id}")
        assert duplicate.json()["proposal_id"] == body["proposal_id"]


def test_dsh_review_requires_parsed_content(monkeypatch):
    node_id = f"dsh-empty-{uuid4()}"
    with TestClient(app) as client:
        client.post("/api/v1/nodes/heartbeat", json={"node_id": node_id, "hostname": node_id, "cpu_percent": 1, "disk_free_bytes": 100})
        file_id = client.post(
            "/api/v1/files/report",
            json={"node_id": node_id, "path": f"docs/{node_id}.txt", "file_hash": "d" * 64, "size_bytes": 3},
        ).json()["id"]
        assert client.post(f"/api/v1/dsh/review?file_id={file_id}").status_code == 409


def test_dsh_proposal_always_marks_human_review_required(monkeypatch):
    monkeypatch.setattr(settings, "embedding_provider", "deterministic")
    node_id = f"dsh-gate-{uuid4()}"
    content = "普通制度文档"
    file_hash = hashlib.sha256(content.encode()).hexdigest()
    with TestClient(app) as client:
        client.post("/api/v1/nodes/heartbeat", json={"node_id": node_id, "hostname": node_id, "cpu_percent": 1, "disk_free_bytes": 100})
        file_id = client.post(
            "/api/v1/files/report",
            json={"node_id": node_id, "path": f"docs/{node_id}.txt", "file_hash": file_hash, "size_bytes": len(content.encode())},
        ).json()["id"]
        client.post(f"/api/v1/files/{file_id}/content", json={"source_node_id": node_id, "file_hash": file_hash, "content": content})
        with SessionLocal() as db:
            from app.services import run_task_once

            run_task_once(db, "dsh-gate-worker")
            run_task_once(db, "dsh-gate-worker")
        proposal_id = client.post(f"/api/v1/dsh/review?file_id={file_id}").json()["proposal_id"]
        body = client.get("/api/v1/proposals").json()
        proposal = next(item for item in body if item["id"] == proposal_id)
        assert '"human_review_required": true' in proposal["body"] or '"human_review_required":true' in proposal["body"]
