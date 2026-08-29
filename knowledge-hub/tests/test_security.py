import os
import time
from uuid import uuid4

os.environ["DATABASE_URL"] = "sqlite:///./test.db"

from fastapi.testclient import TestClient

from app.config import settings
from app.db import SessionLocal
from app.main import app
from app.models import ApiKey
from app.security import authorize, create_api_key, create_token, revoke_api_key, verify_api_key, verify_token


def test_jwt_roundtrip_and_roles():
    token = create_token("admin-user", ["admin", "search"], ttl_seconds=60)
    claims = verify_token(token)
    assert claims is not None
    assert claims["sub"] == "admin-user"
    assert "admin" in claims["roles"]
    assert "search" in claims["roles"]


def test_jwt_rejects_tampering_and_expiry():
    token = create_token("user", ["admin"], ttl_seconds=60)
    tampered = token[:-2] + ("AA" if not token.endswith("AA") else "BB")
    assert verify_token(tampered) is None
    expired = create_token("user", ["admin"], ttl_seconds=-1)
    assert verify_token(expired) is None


def test_api_key_create_verify_revoke():
    with SessionLocal() as db:
        db.query(ApiKey).delete(synchronize_session=False)
        db.commit()
        key, plaintext = create_api_key(db, "search", "test-search", ttl_seconds=3600)
        assert verify_api_key(db, plaintext, "search") is True
        assert verify_api_key(db, plaintext, "admin") is False
        assert verify_api_key(db, "wrong-secret", "search") is False
        revoke_api_key(db, key.key_id)
        assert verify_api_key(db, plaintext, "search") is False


def test_admin_key_endpoints_rotate_and_revoke():
    with TestClient(app) as client:
        created = client.post("/api/v1/admin/keys", json={"role": "search", "label": "ci-search"})
        assert created.status_code == 200
        body = created.json()
        assert body["role"] == "search"
        assert body["api_key"]
        listed = client.get("/api/v1/admin/keys")
        assert any(item["key_id"] == body["key_id"] for item in listed.json())
        revoked = client.post(f"/api/v1/admin/keys/{body['key_id']}/revoke")
        assert revoked.status_code == 200
        assert revoked.json()["active"] is False


def test_production_admin_accepts_db_api_key(monkeypatch):
    from app.security import hash_secret

    monkeypatch.setattr(settings, "app_env", "production")
    monkeypatch.setattr(settings, "admin_api_key", "")
    with SessionLocal() as db:
        db.query(ApiKey).delete(synchronize_session=False)
        db.commit()
        key, plaintext = create_api_key(db, "admin", "prod-admin")
    with TestClient(app) as client:
        assert client.get("/api/v1/nodes").status_code == 401
        response = client.get("/api/v1/nodes", headers={"X-Api-Key": plaintext})
        assert response.status_code == 200


def test_production_admin_accepts_jwt(monkeypatch):
    monkeypatch.setattr(settings, "app_env", "production")
    monkeypatch.setattr(settings, "admin_api_key", "")
    token = create_token("jwt-admin", ["admin"], ttl_seconds=120)
    with TestClient(app) as client:
        assert client.get("/api/v1/nodes").status_code == 401
        assert client.get("/api/v1/nodes", headers={"Authorization": f"Bearer {token}"}).status_code == 200
