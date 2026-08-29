"""Embedding adapters with an explicit deterministic fallback.

The fallback is only a lexical ranking aid for development/offline operation;
responses expose its provider name so operators never mistake it for Ollama.
"""
import hashlib
import json
import math
from dataclasses import dataclass
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from .config import settings


@dataclass
class EmbeddingResult:
    vector: list[float]
    provider: str
    status: str


def _validate_url() -> str:
    parsed = urlparse(settings.embedding_base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise RuntimeError("embedding base URL must be an http(s) URL with a hostname")
    if parsed.hostname.lower() not in settings.embedding_allowed_hostnames:
        raise RuntimeError(f"embedding host is not allowlisted: {parsed.hostname}")
    return settings.embedding_base_url.rstrip("/")


def _deterministic_embedding(text: str) -> list[float]:
    dimension = settings.embedding_dimension
    vector = [0.0] * dimension
    tokens = [text[index:index + 3] for index in range(max(1, len(text) - 2))]
    for token in tokens:
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        index = int.from_bytes(digest[:4], "big") % dimension
        vector[index] += 1.0 if digest[4] & 1 else -1.0
    norm = math.sqrt(sum(value * value for value in vector)) or 1.0
    return [value / norm for value in vector]


def _ollama_embedding(text: str) -> list[float]:
    base = _validate_url()
    request = Request(
        f"{base}/api/embeddings",
        data=json.dumps({"model": settings.embedding_model, "prompt": text}, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=settings.embedding_timeout_seconds) as response:
        payload = json.loads(response.read().decode("utf-8"))
    vector = payload.get("embedding")
    if not isinstance(vector, list) or not vector:
        raise RuntimeError("embedding service returned no embedding")
    vector = [float(value) for value in vector]
    if len(vector) != settings.embedding_dimension:
        raise RuntimeError(f"embedding dimension mismatch: expected {settings.embedding_dimension}, got {len(vector)}")
    return vector


def embed_text(text: str) -> EmbeddingResult:
    if settings.embedding_provider == "deterministic":
        return EmbeddingResult(_deterministic_embedding(text), "deterministic", "degraded")
    try:
        return EmbeddingResult(_ollama_embedding(text), "ollama", "ready")
    except Exception:
        return EmbeddingResult(_deterministic_embedding(text), "deterministic_fallback", "degraded")
