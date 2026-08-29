"""Deterministic LangGraph orchestration for the knowledge pipeline.

The graph owns business workflow order only:

    register -> parse -> embed -> replica

Every node is a plain ``(state) -> partial_state`` callable that opens its own
database session. That makes the same nodes runnable two ways:

* ``ORCHESTRATION_MODE=langgraph`` compiles a real ``langgraph`` StateGraph and
  invokes it.
* ``ORCHESTRATION_MODE=state_machine`` (default) runs the identical nodes
  sequentially, so the system never depends on the optional package being
  importable.

Either way PostgreSQL remains the source of truth; the graph never bypasses
idempotency keys, hash lineage checks, or the human approval gate.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Callable

from sqlalchemy import select

from .config import settings
from .db import SessionLocal
from .embeddings import embed_text
from .models import DocumentChunk, FileRecord, Task

try:  # optional runtime dependency; the sequential fallback is always valid
    from langgraph.graph import END, StateGraph

    _LANGGRAPH_AVAILABLE = True
except Exception:  # pragma: no cover - depends on the optional package
    END = "__end__"
    StateGraph = None  # type: ignore[assignment]
    _LANGGRAPH_AVAILABLE = False


def langgraph_available() -> bool:
    return _LANGGRAPH_AVAILABLE


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _register_node(state: dict[str, Any]) -> dict[str, Any]:
    file_id = int(state["file_id"])
    file_hash = state["file_hash"]
    with SessionLocal() as db:
        record = db.get(FileRecord, file_id)
        if record is None:
            raise ValueError(f"file record not found: {file_id}")
        if record.file_hash != file_hash:
            raise ValueError(f"file hash lineage mismatch for file: {file_id}")
        if record.status not in {"parsed", "parsing", "parse_failed"}:
            record.status = "registered"
        db.commit()
    return {"register": "ok", "registered": True}


def _parse_node(state: dict[str, Any]) -> dict[str, Any]:
    file_id = int(state["file_id"])
    file_hash = state["file_hash"]
    content = state.get("content")
    if content is None:
        return {"parse": "skipped", "reason": "no content supplied to the pipeline"}
    if not isinstance(content, str) or not content:
        raise ValueError(f"pipeline parse content is required for file: {file_id}")
    with SessionLocal() as db:
        record = db.get(FileRecord, file_id)
        if record is None or record.file_hash != file_hash:
            raise ValueError(f"file hash lineage mismatch for file: {file_id}")
        if _sha256(content) != file_hash:
            raise ValueError(f"content hash mismatch for file: {file_id}")
        from .services import _split_text  # local import avoids a cycle at module load

        chunks = _split_text(content, settings.parse_chunk_chars)
        if not chunks:
            raise ValueError(f"file has no parseable text: {file_id}")
        db.query(DocumentChunk).filter(DocumentChunk.file_id == file_id).delete(synchronize_session=False)
        for index, chunk in enumerate(chunks):
            db.add(
                DocumentChunk(
                    file_id=file_id,
                    chunk_index=index,
                    content=chunk,
                    content_hash=_sha256(chunk),
                    file_hash=file_hash,
                )
            )
        record.status = "parsed"
        db.commit()
    return {"parse": "ok", "chunks": len(chunks)}


def _embed_node(state: dict[str, Any]) -> dict[str, Any]:
    file_id = int(state["file_id"])
    file_hash = state["file_hash"]
    providers: set[str] = set()
    with SessionLocal() as db:
        chunks = db.scalars(
            select(DocumentChunk)
            .where(DocumentChunk.file_id == file_id, DocumentChunk.file_hash == file_hash)
            .order_by(DocumentChunk.chunk_index)
        ).all()
        if not chunks:
            return {"embed": "skipped", "reason": "no chunks available"}
        for chunk in chunks:
            result = embed_text(chunk.content)
            chunk.embedding = result.vector
            chunk.embedding_provider = result.provider
            chunk.embedding_status = result.status
            chunk.embedded_at = datetime.now(timezone.utc)
            providers.add(result.provider)
        db.commit()
    return {"embed": "ok", "chunks": len(chunks), "providers": sorted(providers)}


def _replica_node(state: dict[str, Any]) -> dict[str, Any]:
    file_id = int(state["file_id"])
    file_hash = state["file_hash"]
    queued = 0
    with SessionLocal() as db:
        record = db.get(FileRecord, file_id)
        if record is None or record.file_hash != file_hash:
            raise ValueError(f"file hash lineage mismatch for file: {file_id}")
        for node_id in settings.fixed_replica_node_ids:
            task_key = f"replica-repair:{file_id}:{file_hash}:{node_id}"
            existing = db.scalars(select(Task).where(Task.idempotency_key == task_key)).first()
            if existing is None:
                db.add(
                    Task(
                        kind="replica_repair",
                        idempotency_key=task_key,
                        payload_json=json.dumps(
                            {"file_id": file_id, "file_hash": file_hash, "target_node_id": node_id},
                            ensure_ascii=False,
                        ),
                    )
                )
                queued += 1
        db.commit()
    return {"replica": "ok", "repairs_queued": queued, "replica_nodes": settings.fixed_replica_node_ids}


_PIPELINE_NODES: list[tuple[str, Callable[[dict[str, Any]], dict[str, Any]]]] = [
    ("register", _register_node),
    ("parse", _parse_node),
    ("embed", _embed_node),
    ("replica", _replica_node),
]


def build_pipeline_graph():
    """Compile the real LangGraph StateGraph, or raise if the package is absent."""
    if not _LANGGRAPH_AVAILABLE or StateGraph is None:
        raise RuntimeError("langgraph package is not installed; use ORCHESTRATION_MODE=state_machine")
    graph = StateGraph(dict)
    for name, node in _PIPELINE_NODES:
        graph.add_node(name, node)
    graph.set_entry_point(_PIPELINE_NODES[0][0])
    for current, following in zip([name for name, _ in _PIPELINE_NODES], [name for name, _ in _PIPELINE_NODES][1:] + [END]):
        graph.add_edge(current, following)
    return graph.compile()


def _run_sequential(state: dict[str, Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for name, node in _PIPELINE_NODES:
        summary[name] = node(state)
    return summary


def run_file_pipeline(file_id: int, file_hash: str, content: str | None = None) -> dict[str, Any]:
    """Run the deterministic register->parse->embed->replica pipeline for a file."""
    state: dict[str, Any] = {"file_id": file_id, "file_hash": file_hash, "content": content}
    if settings.orchestration_mode == "langgraph" and _LANGGRAPH_AVAILABLE:
        try:
            graph = build_pipeline_graph()
            result = graph.invoke(state)
            return {"orchestration": "langgraph", "stages": result}
        except Exception:
            # The deterministic sequential path is always the safety net.
            return {"orchestration": "state_machine_fallback", "stages": _run_sequential(state)}
    return {"orchestration": "state_machine", "stages": _run_sequential(state)}


def orchestration_status() -> dict[str, Any]:
    return {
        "configured_mode": settings.orchestration_mode,
        "langgraph_available": _LANGGRAPH_AVAILABLE,
        "effective_mode": "langgraph" if settings.orchestration_mode == "langgraph" and _LANGGRAPH_AVAILABLE else "state_machine",
        "pipeline": [name for name, _ in _PIPELINE_NODES],
    }
