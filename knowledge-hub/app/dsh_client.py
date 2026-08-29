"""DeepSeek-Harness (DSH) adapter.

DSH is the *soft* decision brain: document review, content refinement, and
flywheel analysis. Following the system's hard rule, DSH output can only ever
become a pending ``Proposal`` — it can never mutate knowledge data directly.

When DSH is disabled or unreachable the adapter returns an explicit,
deterministic fallback result with ``status="degraded"`` so operators never
mistake it for a real model verdict.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from .config import settings


@dataclass
class DSHResult:
    provider: str
    status: str  # ready | degraded | disabled
    kind: str
    verdict: str
    confidence: float
    summary: str
    raw: dict[str, Any]


def _validate_url() -> str:
    parsed = urlparse(settings.dsh_base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise RuntimeError("DSH base URL must be an http(s) URL with a hostname")
    if parsed.hostname.lower() not in settings.dsh_allowed_hostnames:
        raise RuntimeError(f"DSH host is not allowlisted: {parsed.hostname}")
    return settings.dsh_base_url.rstrip("/")


def _deterministic_review(content: str) -> DSHResult:
    """Offline heuristics only; clearly labelled as degraded."""
    text = content.strip()
    suspicious = []
    if not text:
        suspicious.append("empty document")
    if any(marker in text.lower() for marker in ("password=", "secret_key", "api_key=", "private key")):
        suspicious.append("possible secrets in document")
    verdict = "flag" if suspicious else "ok"
    summary = "；".join(suspicious) if suspicious else "未发现确定性规则层面的明显风险"
    return DSHResult("deterministic_review_fallback", "degraded", "document_review", verdict, 0.5, summary, {"suspicious": suspicious})


def _post_dsh(path: str, payload: dict[str, Any]) -> dict[str, Any]:
    base = _validate_url()
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = Request(
        f"{base}{path}",
        data=body,
        headers={"Content-Type": "application/json", **({"Authorization": f"Bearer {settings.dsh_api_key}"} if settings.dsh_api_key else {})},
        method="POST",
    )
    with urlopen(request, timeout=settings.dsh_timeout_seconds) as response:
        return json.loads(response.read().decode("utf-8"))


def _call(kind: str, payload: dict[str, Any], path: str) -> DSHResult:
    if not settings.dsh_enabled:
        return DSHResult("disabled", "disabled", kind, "skip", 0.0, "DeepSeek-Harness is disabled", {})
    try:
        raw = _post_dsh(path, payload)
        verdict = str(raw.get("verdict") or raw.get("decision") or "unknown")
        confidence = float(raw.get("confidence") or raw.get("score") or 0.0)
        summary = str(raw.get("summary") or raw.get("reason") or json.dumps(raw, ensure_ascii=False))
        return DSHResult("deepseek-harness", "ready", kind, verdict, confidence, summary, raw)
    except Exception as exc:  # degrade, never break the deterministic pipeline
        if kind == "document_review":
            fallback = _deterministic_review(str(payload.get("content", "")))
            fallback.raw = {**fallback.raw, "dsh_error": str(exc)}
            return fallback
        return DSHResult("deterministic_fallback", "degraded", kind, "skip", 0.0, f"DSH unreachable: {exc}", {"dsh_error": str(exc)})


def review_document(title: str, content: str) -> DSHResult:
    return _call("document_review", {"title": title, "content": content}, "/v1/review")


def refine_document(content: str) -> DSHResult:
    return _call("content_refinement", {"content": content}, "/v1/refine")


def analyze_flywheel(gaps: list[dict[str, Any]]) -> DSHResult:
    return _call("flywheel_analysis", {"gaps": gaps}, "/v1/flywheel-analysis")


def status() -> dict[str, Any]:
    return {
        "enabled": settings.dsh_enabled,
        "base_url": settings.dsh_base_url or None,
        "timeout_seconds": settings.dsh_timeout_seconds,
        "allowed_hosts": sorted(settings.dsh_allowed_hostnames),
    }
