"""Identity and RBAC primitives.

Production identity is layered:

* Legacy bootstrap header keys (``X-Admin-Key``, ``X-Node-Key``, ...) keep
  working for backward compatibility and first-boot.
* Database-backed API keys support rotation, revocation, expiry, and per-role
  scopes through ``X-Api-Key``.
* Signed HS256 JWTs carry a principal plus roles through ``Authorization:
  Bearer ...``.
* OIDC discovery is a documented, import-safe hook: operators point
  ``OIDC_DISCOVERY_URL`` at their IdP and verify JWTs with the returned JWKS
  instead of the local shared secret. MFA is enforced at the IdP / reverse
  proxy layer; this module only verifies the resulting identity.

Secrets are always stored as SHA-256 hashes; plaintext is returned exactly once
at creation time.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import settings
from .models import ApiKey, AuditLog

ROLES = {"admin", "node", "mobile", "search", "flywheel", "download", "mqtt_bridge"}


def hash_secret(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def _signing_key() -> bytes:
    if not settings.security_secret:
        return b"knowledge-hub-insecure-dev-secret"
    return settings.security_secret.encode("utf-8")


def create_token(principal: str, roles: list[str], ttl_seconds: int | None = None) -> str:
    """Issue an HS256 JWT without pulling in an external JWT dependency."""
    now = int(time.time())
    ttl = ttl_seconds if ttl_seconds is not None else settings.jwt_ttl_seconds
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {"sub": principal, "roles": [role for role in roles if role in ROLES], "iat": now, "exp": now + ttl}
    signing_input = f"{_b64url(json.dumps(header, separators=(',', ':')).encode())}.{_b64url(json.dumps(payload, separators=(',', ':')).encode())}"
    signature = hmac.new(_signing_key(), signing_input.encode("ascii"), hashlib.sha256).digest()
    return f"{signing_input}.{_b64url(signature)}"


def verify_token(token: str) -> dict[str, Any] | None:
    try:
        header_b64, payload_b64, signature_b64 = token.split(".")
    except ValueError:
        return None
    signing_input = f"{header_b64}.{payload_b64}"
    expected = hmac.new(_signing_key(), signing_input.encode("ascii"), hashlib.sha256).digest()
    try:
        supplied = _b64url_decode(signature_b64)
    except Exception:
        return None
    if not hmac.compare_digest(expected, supplied):
        return None
    try:
        payload = json.loads(_b64url_decode(payload_b64))
    except Exception:
        return None
    if int(payload.get("exp", 0)) < int(time.time()):
        return None
    return payload


def create_api_key(db: Session, role: str, label: str = "", ttl_seconds: int | None = None) -> tuple[ApiKey, str]:
    if role not in ROLES:
        raise ValueError(f"unknown role: {role}")
    plaintext = f"kh_{role}_{secrets.token_urlsafe(32)}"
    key_id = f"{role}-{secrets.token_hex(8)}"
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds) if ttl_seconds is not None else None
    key = ApiKey(key_id=key_id, key_hash=hash_secret(plaintext), role=role, label=label, expires_at=expires_at)
    db.add(key)
    db.flush()
    db.add(AuditLog(actor="admin", action="api_key.created", resource_type="api_key", resource_id=key.key_id, detail=json.dumps({"role": role, "label": label}, ensure_ascii=False)))
    db.commit()
    db.refresh(key)
    return key, plaintext


def revoke_api_key(db: Session, key_id: str) -> ApiKey:
    key = db.scalars(select(ApiKey).where(ApiKey.key_id == key_id)).first()
    if key is None:
        raise ValueError("api key not found")
    key.active = False
    db.add(AuditLog(actor="admin", action="api_key.revoked", resource_type="api_key", resource_id=key.key_id))
    db.commit()
    db.refresh(key)
    return key


def verify_api_key(db: Session, plaintext: str, role: str) -> bool:
    if not plaintext or role not in ROLES:
        return False
    key = db.scalars(select(ApiKey).where(ApiKey.key_hash == hash_secret(plaintext), ApiKey.role == role)).first()
    if key is None or not key.active:
        return False
    if key.expires_at is not None:
        expires = key.expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) > expires:
            return False
    key.last_used_at = datetime.now(timezone.utc)
    db.commit()
    return True


def authorize(request: Request, db: Session, role: str) -> bool:
    """Accept a Bearer JWT or a database-backed API key for the requested role."""
    authorization = request.headers.get("authorization", "")
    if authorization.lower().startswith("bearer "):
        claims = verify_token(authorization[7:].strip())
        if claims and role in (claims.get("roles") or []):
            return True
    api_key = request.headers.get("x-api-key")
    if api_key:
        return verify_api_key(db, api_key, role)
    return False


def oidc_status() -> dict[str, Any]:
    return {
        "configured": bool(settings.oidc_discovery_url),
        "discovery_url": settings.oidc_discovery_url or None,
        "client_id": settings.oidc_client_id or None,
        "issuer": settings.oidc_issuer or None,
        "local_jwt_ready": bool(settings.security_secret),
    }
