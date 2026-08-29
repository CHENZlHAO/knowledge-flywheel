import hashlib
import os
from uuid import uuid4

os.environ["DATABASE_URL"] = "sqlite:///./test.db"

from fastapi.testclient import TestClient

from app import langgraph_flow
from app.config import settings
from app.db import SessionLocal
from app.main import app
from app.models import DocumentChunk, FileRecord, Node, Task
from app.services import run_task_once


def _register_replica_node(node_id: str) -> None:
    with SessionLocal() as db:
        db.add(Node(id=node_id, hostname=node_id, is_replica=True))
        db.commit()


def test_orchestration_status_is_explicit():
    status = langgraph_flow.orchestration_status()
    assert status["configured_mode"] == settings.orchestration_mode
    assert status["effective_mode"] in {"langgraph", "state_machine"}
    assert status["pipeline"] == ["register", "parse", "embed", "replica"]
    assert status["langgraph_available"] is langgraph_flow.langgraph_available()


def test_pipeline_runs_end_to_end(monkeypatch):
    monkeypatch.setattr(settings, "embedding_provider", "deterministic")
    monkeypatch.setattr(settings, "orchestration_mode", "state_machine")
    node_id = f"pipeline-node-{uuid4()}"
    replica_id = f"pipeline-replica-{uuid4()}"
    _register_replica_node(replica_id)
    monkeypatch.setattr(settings, "replica_node_ids", replica_id)

    content = "报销流程\n\n提交发票后由直属主管审批。"
    file_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()

    with TestClient(app) as client:
        client.post("/api/v1/nodes/heartbeat", json={"node_id": node_id, "hostname": node_id, "cpu_percent": 1, "disk_free_bytes": 100})
        file_id = client.post(
            "/api/v1/files/report",
            json={"node_id": node_id, "path": f"docs/{node_id}.md", "file_hash": file_hash, "size_bytes": len(content.encode())},
        ).json()["id"]

        result = langgraph_flow.run_file_pipeline(file_id, file_hash, content)
        assert result["orchestration"] == "state_machine"
        assert result["stages"]["register"]["registered"] is True
        assert result["stages"]["parse"]["chunks"] >= 1
        assert result["stages"]["embed"]["chunks"] >= 1
        assert result["stages"]["replica"]["repairs_queued"] >= 1

    with SessionLocal() as db:
        record = db.get(FileRecord, file_id)
        assert record.status == "parsed"
        chunks = db.query(DocumentChunk).filter(DocumentChunk.file_id == file_id).all()
        assert chunks and all(chunk.embedding is not None for chunk in chunks)
        repairs = db.query(Task).filter(Task.kind == "replica_repair", Task.payload_json.contains(str(file_id))).all()
        assert repairs


def test_pipeline_run_task_endpoint_and_worker(monkeypatch):
    monkeypatch.setattr(settings, "embedding_provider", "deterministic")
    monkeypatch.setattr(settings, "orchestration_mode", "state_machine")
    node_id = f"pipeline-task-node-{uuid4()}"
    with TestClient(app) as client:
        client.post("/api/v1/nodes/heartbeat", json={"node_id": node_id, "hostname": node_id, "cpu_percent": 1, "disk_free_bytes": 100})
        content = "差旅制度\n\n住宿费由部门预算承担。"
        file_hash = hashlib.sha256(content.encode()).hexdigest()
        file_id = client.post(
            "/api/v1/files/report",
            json={"node_id": node_id, "path": f"docs/{node_id}.txt", "file_hash": file_hash, "size_bytes": len(content.encode())},
        ).json()["id"]
        key = f"pipeline-run-{uuid4()}"
        queued = client.post(
            "/api/v1/pipeline/run",
            json={"file_id": file_id, "file_hash": file_hash, "content": content, "idempotency_key": key},
        )
        assert queued.status_code == 200
        assert queued.json()["status"] == "pending"
        duplicate = client.post(
            "/api/v1/pipeline/run",
            json={"file_id": file_id, "file_hash": file_hash, "content": content, "idempotency_key": key},
        )
        assert duplicate.json()["task_id"] == queued.json()["task_id"]

        # The file report already enqueued a file_register task, so the worker
        # claims FIFO; drain both tasks until the pipeline_run task succeeds.
        for _ in range(10):
            with SessionLocal() as db:
                run_task_once(db, "pipeline-run-worker", lease_seconds=60)
                target = db.get(Task, queued.json()["task_id"])
                if target is not None and target.status == "success":
                    break
        status = client.get("/api/v1/orchestration/status")
        assert status.status_code == 200
        pipeline = {item["id"]: item for item in client.get("/api/v1/pipeline/files").json()}
        assert pipeline[file_id]["status"] == "parsed"


def test_build_pipeline_graph_degrades_cleanly():
    if langgraph_flow.langgraph_available():
        graph = langgraph_flow.build_pipeline_graph()
        assert graph is not None
    else:
        try:
            langgraph_flow.build_pipeline_graph()
        except RuntimeError as exc:
            assert "langgraph" in str(exc).lower()
        else:
            raise AssertionError("expected RuntimeError when langgraph is not installed")
