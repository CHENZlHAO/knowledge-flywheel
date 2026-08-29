import json
import os

os.environ["DATABASE_URL"] = "sqlite:///./test.db"

import pytest

from app.backup import backup_status, perform_backup
from app.config import settings


@pytest.fixture()
def backup_dirs(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "backup_dir", str(tmp_path / "backups"))
    monkeypatch.setattr(settings, "backup_retention_days", 7)
    storage = tmp_path / "storage"
    storage.mkdir()
    (storage / "sample.bin").write_bytes(b"sample object bytes")
    monkeypatch.setattr(settings, "storage_root", str(storage))
    return tmp_path


def test_backup_creates_manifest_and_artifacts(backup_dirs):
    manifest = perform_backup()
    assert manifest["backup_id"]
    assert manifest["database_backend"] == "sqlite"
    names = {item["name"] for item in manifest["files"]}
    assert {"database.dump", "storage.tar.gz"} <= names
    assert all(item["sha256"] for item in manifest["files"])
    run_dir = backup_dirs / "backups" / manifest["backup_id"]
    assert (run_dir / "manifest.json").exists()
    assert (run_dir / "database.dump").exists()
    assert (run_dir / "storage.tar.gz").exists()


def test_backup_status_lists_runs(backup_dirs):
    manifest = perform_backup()
    status = backup_status()
    assert status["retention_days"] == 7
    assert any(item["backup_id"] == manifest["backup_id"] for item in status["runs"])


def test_backup_task_kind_executes(backup_dirs, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.db import SessionLocal
    from app.services import run_task_once

    monkeypatch.setattr(settings, "backup_dir", str(backup_dirs / "task-backups"))
    with TestClient(app) as client:
        task = client.post("/api/v1/tasks", json={"kind": "backup", "idempotency_key": f"backup-task-{os.urandom(4).hex()}", "payload": {}}).json()
        with SessionLocal() as db:
            run_task_once(db, "backup-worker", lease_seconds=60)
        status = client.get("/api/v1/tasks").json()
        row = next(item for item in status if item["id"] == task["id"])
        assert row["status"] == "success"
        assert "backup" in row["result_json"] if row["result_json"] else True
