import hashlib
import os
from uuid import uuid4

os.environ["DATABASE_URL"] = "sqlite:///./test.db"

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app


@pytest.fixture()
def storage_root(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "storage_backend", "local")
    monkeypatch.setattr(settings, "storage_root", str(tmp_path / "store"))
    return tmp_path / "store"


def _report_file(client, node_id, path, content: bytes):
    file_hash = hashlib.sha256(content).hexdigest()
    response = client.post(
        "/api/v1/files/report",
        json={"node_id": node_id, "path": path, "file_hash": file_hash, "size_bytes": len(content)},
    )
    return response.json()["id"], file_hash


def test_blob_roundtrip_and_dedup(storage_root):
    node_id = f"blob-{uuid4()}"
    payload = b"%PDF-1.4 fake binary \x00\x01\x02 content"
    with TestClient(app) as client:
        client.post("/api/v1/nodes/heartbeat", json={"node_id": node_id, "hostname": node_id, "cpu_percent": 1, "disk_free_bytes": 100})
        file_id, file_hash = _report_file(client, node_id, f"docs/{node_id}.pdf", payload)
        headers = {"X-File-Hash": file_hash, "X-Node-Id": node_id, "Content-Type": "application/pdf"}
        first = client.post(f"/api/v1/files/{file_id}/blob", content=payload, headers=headers)
        assert first.status_code == 200
        body = first.json()
        assert body["sha256"] == file_hash
        assert body["storage_backend"] == "local"
        second = client.post(f"/api/v1/files/{file_id}/blob", content=payload, headers=headers)
        assert second.json()["blob_id"] == body["blob_id"]

        download = client.get(f"/api/v1/gateway/files/{file_id}/binary", headers={"X-Download-Key": settings.download_api_key or ""})
        assert download.status_code == 200
        assert download.content == payload
        assert download.headers["x-file-hash"] == file_hash
        assert download.headers["etag"] == f'"{file_hash}"'


def test_blob_upload_rejects_hash_mismatch(storage_root):
    node_id = f"blob-bad-{uuid4()}"
    with TestClient(app) as client:
        client.post("/api/v1/nodes/heartbeat", json={"node_id": node_id, "hostname": node_id, "cpu_percent": 1, "disk_free_bytes": 100})
        file_id, _ = _report_file(client, node_id, f"docs/{node_id}.bin", b"expected-bytes")
        headers = {"X-File-Hash": "f" * 64, "X-Node-Id": node_id}
        response = client.post(f"/api/v1/files/{file_id}/blob", content=b"other-bytes", headers=headers)
        assert response.status_code == 409


def test_binary_download_rejects_superseded_version(storage_root):
    node_id = f"blob-superseded-{uuid4()}"
    payload = b"old version bytes"
    with TestClient(app) as client:
        client.post("/api/v1/nodes/heartbeat", json={"node_id": node_id, "hostname": node_id, "cpu_percent": 1, "disk_free_bytes": 100})
        file_id, file_hash = _report_file(client, node_id, f"docs/{node_id}.doc", payload)
        client.post(f"/api/v1/files/{file_id}/blob", content=payload, headers={"X-File-Hash": file_hash, "X-Node-Id": node_id})
        new_payload = b"new version bytes"
        new_id, _ = _report_file(client, node_id, f"docs/{node_id}.doc", new_payload)
        assert new_id != file_id
        response = client.get(f"/api/v1/gateway/files/{file_id}/binary")
        assert response.status_code == 404
