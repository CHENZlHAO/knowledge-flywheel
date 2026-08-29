import json
import hashlib
import re
import math
from uuid import uuid4
from datetime import datetime, timedelta, timezone
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session
from .config import settings
from .models import Node, FileRecord, FileReplica, DocumentChunk, BlobObject, Task, Proposal, AuditLog, RemoteCommand, Alert, FlywheelEvent
from .embeddings import embed_text
from . import dispatcher as _task_dispatcher  # registers post-commit task publication hooks


TASK_TRANSITIONS = {
    "pending": {"running", "cancelled"},
    "running": {"success", "failed", "pending"},
    "waiting": {"success", "failed", "pending"},
    "failed": {"pending"},
    "success": set(),
    "cancelled": set(),
}


def _normalize_query(query: str) -> str:
    # Collapse all Unicode whitespace so equivalent CJK queries aggregate together.
    return "".join(query.casefold().split())


def _record_flywheel_event(db: Session, *, idempotency_key: str, event_type: str, query: str, actor: str, rating: int | None = None, result_count: int | None = None, comment: str | None = None, metadata: dict | None = None) -> FlywheelEvent:
    existing = db.scalars(select(FlywheelEvent).where(FlywheelEvent.idempotency_key == idempotency_key)).first()
    if existing is not None:
        return existing
    event = FlywheelEvent(idempotency_key=idempotency_key, event_type=event_type, query=query, normalized_query=_normalize_query(query), rating=rating, result_count=result_count, comment=comment, actor=actor, metadata_json=json.dumps(metadata or {}, ensure_ascii=False))
    db.add(event)
    db.commit()
    db.refresh(event)
    return event


def record_flywheel_feedback(db: Session, data) -> FlywheelEvent:
    return _record_flywheel_event(db, idempotency_key=data.idempotency_key, event_type="feedback", query=data.query, rating=data.rating, comment=data.comment, actor=data.actor, metadata=data.metadata)


def record_flywheel_retrieval(db: Session, data) -> FlywheelEvent:
    return _record_flywheel_event(db, idempotency_key=data.idempotency_key, event_type="retrieval", query=data.query, result_count=data.result_count, actor=data.actor, metadata=data.metadata)


def aggregate_flywheel_gaps(db: Session, limit: int = 50) -> list[dict]:
    rows = db.scalars(select(FlywheelEvent).order_by(FlywheelEvent.created_at, FlywheelEvent.id)).all()
    grouped: dict[str, dict] = {}
    for event in rows:
        item = grouped.setdefault(event.normalized_query, {"query": event.query, "normalized_query": event.normalized_query, "retrieval_count": 0, "no_result_count": 0, "negative_feedback_count": 0, "positive_feedback_count": 0, "score": 0, "last_seen_at": event.created_at})
        item["last_seen_at"] = max(item["last_seen_at"], event.created_at)
        if event.event_type == "retrieval":
            item["retrieval_count"] += 1
            if event.result_count == 0:
                item["no_result_count"] += 1
                item["score"] += 2
        elif event.event_type == "feedback":
            if event.rating is not None and event.rating <= 2:
                item["negative_feedback_count"] += 1
                item["score"] += 1
            elif event.rating is not None and event.rating >= 4:
                item["positive_feedback_count"] += 1
    return sorted(grouped.values(), key=lambda item: (-item["score"], -item["no_result_count"], item["normalized_query"]))[:limit]


def create_flywheel_proposal(db: Session, query: str) -> Proposal:
    normalized = _normalize_query(query)
    gap = next((item for item in aggregate_flywheel_gaps(db, 200) if item["normalized_query"] == normalized), None)
    if gap is None:
        raise ValueError("flywheel gap not found")
    title = f"知识缺口优化：{gap['query'][:180]}"
    existing = db.scalars(
        select(Proposal).where(
            Proposal.kind == "flywheel_optimization",
            Proposal.title == title,
            Proposal.status == "pending",
        ).order_by(Proposal.created_at.desc())
    ).first()
    if existing is not None:
        return existing
    body = json.dumps({"source": "deterministic_flywheel_aggregation", "gap": gap, "human_review_required": True}, ensure_ascii=False, default=str)
    proposal = Proposal(kind="flywheel_optimization", title=title, body=body, created_by="flywheel")
    db.add(proposal)
    db.commit()
    db.refresh(proposal)
    return proposal


def _cosine(left: list[float], right: list[float]) -> float:
    numerator = sum(a * b for a, b in zip(left, right))
    denominator = math.sqrt(sum(a * a for a in left)) * math.sqrt(sum(b * b for b in right))
    return numerator / denominator if denominator else 0.0


def search_knowledge(db: Session, query: str, *, top_k: int, idempotency_key: str | None, actor: str) -> dict:
    if not query.strip():
        raise ValueError("query must not be blank")
    embedding = embed_text(query)
    if db.bind is not None and db.bind.dialect.name == "postgresql":
        vector_literal = "[" + ",".join(format(value, ".9g") for value in embedding.vector) + "]"
        rows = db.execute(text("""
            SELECT f.id AS file_id, f.path, f.file_hash, f.version, dc.id AS chunk_id,
                   dc.chunk_index, dc.content, dc.embedding_provider, dc.embedding_status,
                   1 - (dc.embedding <=> CAST(:query_vector AS vector)) AS score
            FROM document_chunks dc JOIN files f ON f.id = dc.file_id
            WHERE f.alive = true AND f.file_hash = dc.file_hash AND dc.embedding IS NOT NULL
            ORDER BY dc.embedding <=> CAST(:query_vector AS vector), f.id, dc.chunk_index
            LIMIT :top_k
        """), {"query_vector": vector_literal, "top_k": top_k}).mappings().all()
        hits = [{**dict(row), "score": round(float(row["score"]), 6)} for row in rows]
    else:
        rows = db.execute(
            select(DocumentChunk, FileRecord)
            .join(FileRecord, FileRecord.id == DocumentChunk.file_id)
            .where(FileRecord.alive.is_(True), FileRecord.file_hash == DocumentChunk.file_hash, DocumentChunk.embedding.is_not(None))
        ).all()
        scored = []
        for chunk, record in rows:
            vector = chunk.embedding
            if isinstance(vector, str):
                try:
                    vector = json.loads(vector)
                except json.JSONDecodeError:
                    continue
            scored.append((_cosine(embedding.vector, list(vector)), chunk, record))
        scored.sort(key=lambda item: (-item[0], item[1].file_id, item[1].chunk_index))
        hits = [{
            "file_id": record.id, "path": record.path, "file_hash": record.file_hash,
            "version": record.version, "chunk_id": chunk.id, "chunk_index": chunk.chunk_index,
            "content": chunk.content, "score": round(score, 6),
            "embedding_provider": chunk.embedding_provider, "embedding_status": chunk.embedding_status,
        } for score, chunk, record in scored[:top_k]]
    key = idempotency_key or f"search:{uuid4()}"
    event = record_flywheel_retrieval(db, type("Retrieval", (), {"idempotency_key": key, "query": query, "result_count": len(hits), "actor": actor, "metadata": {"embedding_provider": embedding.provider, "embedding_status": embedding.status, "top_k": top_k}})())
    return {"query": query, "count": len(hits), "embedding_provider": embedding.provider, "embedding_status": embedding.status, "results": hits, "retrieval_event_id": event.id}


def _upsert_alert(db: Session, *, fingerprint: str, severity: str, kind: str,
                  resource_type: str, resource_id: str, message: str) -> Alert:
    now = datetime.now(timezone.utc)
    alert = db.scalars(select(Alert).where(Alert.fingerprint == fingerprint)).first()
    if alert is None:
        alert = Alert(fingerprint=fingerprint, severity=severity, kind=kind,
                      resource_type=resource_type, resource_id=resource_id,
                      message=message, status="open", first_seen_at=now, last_seen_at=now)
        db.add(alert)
    else:
        alert.last_seen_at = now
        alert.message = message
        alert.severity = severity
        if alert.status == "resolved":
            alert.status = "open"
            alert.resolved_at = None
    from . import alerting  # local import keeps the notifier stack optional

    alerting.ensure_deliveries(db, alert)
    return alert


def _resolve_alert(db: Session, fingerprint: str) -> None:
    alert = db.scalars(select(Alert).where(Alert.fingerprint == fingerprint)).first()
    if alert is not None and alert.status != "resolved":
        alert.status = "resolved"
        alert.resolved_at = datetime.now(timezone.utc)


def acknowledge_alert(db: Session, alert: Alert, actor: str, status: str) -> Alert:
    if status not in {"acknowledged", "resolved"}:
        raise ValueError("alert status must be acknowledged or resolved")
    if alert.status == "resolved":
        raise ValueError("alert already resolved")
    now = datetime.now(timezone.utc)
    alert.status = status
    alert.acknowledged_by = actor
    alert.acknowledged_at = now
    if status == "resolved":
        alert.resolved_at = now
    db.add(AuditLog(actor=actor, action=f"alert.{status}", resource_type="alert", resource_id=str(alert.id)))
    db.commit()
    db.refresh(alert)
    return alert


def _audit_task(db: Session, task: Task, action: str, actor: str, detail: dict | None = None) -> None:
    db.add(AuditLog(
        actor=actor,
        action=f"task.{action}",
        resource_type="task",
        resource_id=str(task.id),
        detail=json.dumps(detail or {}, ensure_ascii=False),
    ))


def heartbeat(db: Session, data) -> Node:
    node = db.get(Node, data.node_id) or Node(id=data.node_id, hostname=data.hostname)
    node.hostname = data.hostname
    node.ip_address = data.ip_address
    node.agent_version = data.agent_version
    node.cpu_percent = data.cpu_percent
    node.disk_free_bytes = data.disk_free_bytes
    node.is_replica = data.is_replica
    node.status = "online"
    node.last_seen_at = datetime.now(timezone.utc)
    db.add(node)
    _resolve_alert(db, f"node.offline:{data.node_id}")
    db.commit()
    db.refresh(node)
    return node


def apply_node_status_event(db: Session, data) -> Node:
    node = db.get(Node, data.node_id) or Node(id=data.node_id, hostname=data.hostname or data.node_id)
    node.hostname = data.hostname or node.hostname
    node.ip_address = data.ip_address
    node.agent_version = data.agent_version
    node.cpu_percent = data.cpu_percent
    node.disk_free_bytes = data.disk_free_bytes
    node.is_replica = data.is_replica
    node.status = data.status
    node.last_seen_at = datetime.now(timezone.utc)
    db.add(node)
    if data.status == "online":
        _resolve_alert(db, f"node.offline:{data.node_id}")
    else:
        _upsert_alert(db, fingerprint=f"node.offline:{data.node_id}", severity="high", kind="node_offline", resource_type="node", resource_id=data.node_id, message=f"节点 {data.node_id} 已离线：{data.reason or 'status event'}")
    db.flush()
    db.add(AuditLog(
        actor="mqtt-bridge",
        action=f"node.{data.status}",
        resource_type="node",
        resource_id=data.node_id,
        detail=json.dumps({"reason": data.reason} if data.reason else {}, ensure_ascii=False),
    ))
    db.commit()
    db.refresh(node)
    return node


def refresh_node_statuses(db: Session, offline_after: int) -> None:
    now = datetime.now(timezone.utc)
    for node in db.scalars(select(Node)).all():
        seen = node.last_seen_at
        if seen.tzinfo is None:
            seen = seen.replace(tzinfo=timezone.utc)
        if (now - seen).total_seconds() > offline_after:
            node.status = "offline"
            _upsert_alert(db, fingerprint=f"node.offline:{node.id}", severity="high", kind="node_offline", resource_type="node", resource_id=node.id, message=f"节点 {node.id} 超过 {offline_after} 秒未上报")
        elif node.status == "online":
            _resolve_alert(db, f"node.offline:{node.id}")
    db.commit()


def report_file(db: Session, data) -> FileRecord:
    now = datetime.now(timezone.utc)
    existing = db.scalars(select(FileRecord).where(FileRecord.path == data.path, FileRecord.file_hash == data.file_hash)).first()
    if existing:
        existing.source_node_id = data.node_id
        existing.size_bytes = data.size_bytes
        existing.alive = True
        existing.last_seen_at = now
        task_key = f"file-register:{existing.id}:{existing.file_hash}"
        task = db.scalars(select(Task).where(Task.idempotency_key == task_key)).first()
        parse_task = db.scalars(select(Task).where(Task.idempotency_key == f"file-parse:{existing.id}:{existing.file_hash}")).first()
        if parse_task is not None and parse_task.status == "success":
            existing.status = "parsed"
        elif task is not None and task.status == "success" and existing.status not in {"parsing", "parse_failed"}:
            existing.status = "registered"
        elif existing.status == "missing":
            existing.status = "reported"
            _resolve_alert(db, f"file.missing:{existing.id}")
        if existing.status not in {"registered", "parsing", "parsed"}:
            existing.status = "reported"
        if task is None and existing.status not in {"registered", "parsing", "parsed"}:
            db.add(Task(
                kind="file_register",
                idempotency_key=task_key,
                payload_json=json.dumps({"file_id": existing.id, "file_hash": existing.file_hash}, ensure_ascii=False),
            ))
        elif existing.status not in {"registered", "parsing", "parsed"} and task is not None and task.status == "failed":
            task.status = "pending"
            task.error = None
            task.result_json = None
            _audit_task(db, task, "requeued", data.node_id, {"reason": "file_report_repeated"})
        _upsert_file_replica(db, existing, data.node_id, data.file_hash, now)
        db.commit()
        db.refresh(existing)
        return existing
    prior_versions = db.scalars(select(FileRecord).where(FileRecord.path == data.path).order_by(FileRecord.version.desc(), FileRecord.id.desc())).all()
    next_version = (prior_versions[0].version + 1) if prior_versions else 1
    for prior in prior_versions:
        if prior.alive:
            prior.alive = False
            prior.status = "superseded"
            db.add(AuditLog(actor=data.node_id, action="file.superseded", resource_type="file", resource_id=str(prior.id), detail=json.dumps({"new_file_hash": data.file_hash}, ensure_ascii=False)))
    record = FileRecord(path=data.path, file_hash=data.file_hash, size_bytes=data.size_bytes, source_node_id=data.node_id, alive=True, last_seen_at=now, version=next_version)
    db.add(record)
    db.flush()
    db.add(Task(
        kind="file_register",
        idempotency_key=f"file-register:{record.id}:{record.file_hash}",
        payload_json=json.dumps({"file_id": record.id, "file_hash": record.file_hash}, ensure_ascii=False),
    ))
    _upsert_file_replica(db, record, data.node_id, data.file_hash, now)
    db.commit()
    db.refresh(record)
    return record


def _upsert_file_replica(db: Session, record: FileRecord, node_id: str, file_hash: str, now: datetime) -> FileReplica:
    replica = db.scalars(select(FileReplica).where(FileReplica.file_id == record.id, FileReplica.node_id == node_id)).first()
    if replica is None:
        replica = FileReplica(file_id=record.id, node_id=node_id, file_hash=file_hash, status="healthy", last_seen_at=now)
        db.add(replica)
    else:
        replica.file_hash = file_hash
        replica.status = "healthy"
        replica.last_seen_at = now
    return replica


def reconcile_file_liveness(db: Session, missing_after: int, replica_node_ids: list[str] | None = None) -> dict:
    """Mark files missing when their source node has not reported them recently."""
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(seconds=missing_after)
    marked_missing = 0
    recovered = 0
    replica_missing = 0
    repair_queued = 0
    invalid_replica_nodes = []
    desired = []
    for node_id in replica_node_ids or []:
        node = db.get(Node, node_id)
        if node is None or not node.is_replica:
            invalid_replica_nodes.append(node_id)
        else:
            desired.append(node_id)
    for record in db.scalars(select(FileRecord)).all():
        if record.status == "superseded":
            continue
        seen = record.last_seen_at
        if seen.tzinfo is None:
            seen = seen.replace(tzinfo=timezone.utc)
        if seen < cutoff:
            if record.alive or record.status != "missing":
                record.alive = False
                record.status = "missing"
                db.add(AuditLog(actor="system", action="file.missing", resource_type="file", resource_id=str(record.id), detail=json.dumps({"path": record.path}, ensure_ascii=False)))
                _upsert_alert(db, fingerprint=f"file.missing:{record.id}", severity="high", kind="file_missing", resource_type="file", resource_id=str(record.id), message=f"文件 {record.path} 在 {missing_after} 秒内未被源节点确认")
                marked_missing += 1
        elif not record.alive:
            record.alive = True
            if record.status == "missing":
                record.status = "reported"
            db.add(AuditLog(actor="system", action="file.recovered", resource_type="file", resource_id=str(record.id), detail=json.dumps({"path": record.path}, ensure_ascii=False)))
            _resolve_alert(db, f"file.missing:{record.id}")
            recovered += 1
        for replica in db.scalars(select(FileReplica).where(FileReplica.file_id == record.id)).all():
            replica_seen = replica.last_seen_at
            if replica_seen.tzinfo is None:
                replica_seen = replica_seen.replace(tzinfo=timezone.utc)
            if replica_seen < cutoff and replica.status != "missing":
                replica.status = "missing"
                replica_missing += 1
                db.add(AuditLog(actor="system", action="file_replica.missing", resource_type="file_replica", resource_id=str(replica.id), detail=json.dumps({"file_id": record.id, "node_id": replica.node_id}, ensure_ascii=False)))
        if record.alive and desired:
            healthy_nodes = {replica.node_id for replica in db.scalars(select(FileReplica).where(FileReplica.file_id == record.id, FileReplica.status == "healthy")).all()}
            for node_id in desired:
                if node_id in healthy_nodes:
                    continue
                task_key = f"replica-repair:{record.id}:{record.file_hash}:{node_id}"
                task = db.scalars(select(Task).where(Task.idempotency_key == task_key)).first()
                if task is None:
                    db.add(Task(kind="replica_repair", idempotency_key=task_key, payload_json=json.dumps({"file_id": record.id, "file_hash": record.file_hash, "target_node_id": node_id}, ensure_ascii=False)))
                    repair_queued += 1
    db.commit()
    return {"checked": db.scalar(select(func.count(FileRecord.id))) or 0, "marked_missing": marked_missing, "recovered": recovered, "replica_missing": replica_missing, "repair_queued": repair_queued, "invalid_replica_nodes": invalid_replica_nodes, "cutoff": cutoff}


def replica_policy_health(db: Session, node_ids: list[str]) -> dict:
    valid_nodes = []
    invalid_nodes = []
    for node_id in node_ids:
        node = db.get(Node, node_id)
        if node is not None and node.is_replica:
            valid_nodes.append(node_id)
        else:
            invalid_nodes.append(node_id)
    total_files = db.scalar(select(func.count(FileRecord.id)).where(FileRecord.alive.is_(True))) or 0
    healthy_replicas = db.scalar(select(func.count(FileReplica.id)).where(FileReplica.status == "healthy", FileReplica.node_id.in_(valid_nodes))) if valid_nodes else 0
    expected_replicas = total_files * len(valid_nodes)
    pending_repairs = db.scalar(select(func.count(Task.id)).where(Task.kind == "replica_repair", Task.status.in_(["pending", "running", "waiting"]))) or 0
    failed_repairs = db.scalar(select(func.count(Task.id)).where(Task.kind == "replica_repair", Task.status == "failed")) or 0
    return {
        "configured_nodes": node_ids,
        "valid_nodes": valid_nodes,
        "invalid_nodes": invalid_nodes,
        "total_files": total_files,
        "healthy_replicas": healthy_replicas or 0,
        "expected_replicas": expected_replicas,
        "health_rate": round((healthy_replicas or 0) / expected_replicas, 4) if expected_replicas else 1,
        "pending_repairs": pending_repairs,
        "failed_repairs": failed_repairs,
    }


def _split_text(text: str, max_chars: int) -> list[str]:
    if max_chars <= 0:
        raise ValueError("parse chunk size must be greater than zero")
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        return []
    paragraphs = [part.strip() for part in re.split(r"\n{2,}", normalized) if part.strip()]
    chunks: list[str] = []
    current = ""
    for paragraph in paragraphs:
        while len(paragraph) > max_chars:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(paragraph[:max_chars])
            paragraph = paragraph[max_chars:]
        if not paragraph:
            continue
        candidate = f"{current}\n\n{paragraph}" if current else paragraph
        if current and len(candidate) > max_chars:
            chunks.append(current)
            current = paragraph
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def queue_file_content_parse(db: Session, file_id: int, source_node_id: str, file_hash: str, content: str) -> Task:
    record = db.get(FileRecord, file_id)
    if record is None:
        raise ValueError("file not found")
    if record.source_node_id != source_node_id:
        raise ValueError("file source node mismatch")
    if record.file_hash != file_hash:
        raise ValueError("file hash mismatch")
    actual_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    if actual_hash != file_hash:
        raise ValueError("content hash does not match file hash")
    task_key = f"file-parse:{record.id}:{record.file_hash}"
    task = db.scalars(select(Task).where(Task.idempotency_key == task_key)).first()
    if task is None:
        task = Task(
            kind="file_parse",
            idempotency_key=task_key,
            payload_json=json.dumps({"file_id": record.id, "file_hash": record.file_hash, "content": content}, ensure_ascii=False),
        )
        db.add(task)
    elif task.status == "failed":
        task.status = "pending"
        task.error = None
        task.result_json = None
    if record.status != "parsed":
        record.status = "parsing"
    db.commit()
    db.refresh(task)
    return task


def create_task(db: Session, data) -> Task:
    existing = db.scalars(select(Task).where(Task.idempotency_key == data.idempotency_key)).first()
    if existing:
        return existing
    task = Task(kind=data.kind, idempotency_key=data.idempotency_key, payload_json=json.dumps(data.payload, ensure_ascii=False))
    db.add(task)
    db.commit()
    db.refresh(task)
    return task


def transition_task(db: Session, task: Task, new_status: str, error: str | None = None) -> Task:
    if new_status not in TASK_TRANSITIONS.get(task.status, set()):
        raise ValueError(f"invalid task transition {task.status} -> {new_status}")
    task.status = new_status
    task.error = error
    if new_status != "running":
        task.claimed_at = None
        task.claimed_by = None
    if new_status == "running":
        task.attempts += 1
    db.commit()
    db.refresh(task)
    return task


def claim_next_task(db: Session, worker_id: str, lease_seconds: int = 120) -> Task | None:
    """Claim one pending task and recover tasks whose worker lease expired."""
    now = datetime.now(timezone.utc)
    lease_deadline = now - timedelta(seconds=lease_seconds)
    stale = db.scalars(
        select(Task).where(
            Task.status == "running",
            (Task.claimed_at.is_(None) | (Task.claimed_at < lease_deadline)),
        )
    ).all()
    for task in stale:
        task.status = "pending"
        task.claimed_at = None
        task.claimed_by = None
        task.error = "worker lease expired; task requeued"
        _audit_task(db, task, "requeued", "system", {"reason": "lease_expired"})

    statement = (
        select(Task)
        .where(Task.status == "pending")
        .order_by(Task.created_at, Task.id)
        .with_for_update(skip_locked=True)
    )
    task = db.scalars(statement).first()
    if not task:
        db.commit()
        return None
    task.status = "running"
    task.attempts += 1
    task.claimed_at = now
    task.claimed_by = worker_id
    task.error = None
    _audit_task(db, task, "claimed", worker_id)
    db.commit()
    db.refresh(task)
    return task


def claim_task_by_id(db: Session, task_id: int, worker_id: str, lease_seconds: int = 120) -> Task | None:
    """Claim one specific persisted task for a Celery delivery.

    The same lease and state rules as the polling worker apply. A duplicate
    broker delivery simply returns ``None`` once another consumer has claimed
    or completed the task.
    """
    now = datetime.now(timezone.utc)
    lease_deadline = now - timedelta(seconds=lease_seconds)
    task = db.scalar(select(Task).where(Task.id == task_id).with_for_update())
    if task is None:
        return None
    claimed_at = task.claimed_at
    if claimed_at is not None and claimed_at.tzinfo is None:
        claimed_at = claimed_at.replace(tzinfo=timezone.utc)
    if task.status == "running" and claimed_at is not None and claimed_at >= lease_deadline:
        return None
    if task.status not in {"pending", "running"}:
        return None
    if task.status == "running":
        task.error = "worker lease expired; task requeued"
        _audit_task(db, task, "requeued", worker_id, {"reason": "celery_lease_expired"})
    task.status = "running"
    task.attempts += 1
    task.claimed_at = now
    task.claimed_by = worker_id
    task.error = None
    _audit_task(db, task, "claimed", worker_id, {"delivery": "celery"})
    db.commit()
    db.refresh(task)
    return task


def execute_task_payload(db: Session, task: Task) -> dict:
    """Execute only deterministic built-ins until LangGraph adapters are installed."""
    payload = json.loads(task.payload_json or "{}")
    if task.kind == "noop":
        return {"execution": "noop", "payload": payload}
    if task.kind == "file_register":
        file_id = payload.get("file_id")
        expected_hash = payload.get("file_hash")
        record = db.get(FileRecord, file_id)
        if record is None:
            raise ValueError(f"file record not found: {file_id}")
        if record.file_hash != expected_hash:
            raise ValueError(f"file hash lineage mismatch for file: {file_id}")
        record.status = "registered"
        return {"execution": "file_register", "file_id": record.id, "file_hash": record.file_hash, "status": record.status}
    if task.kind == "file_parse":
        file_id = payload.get("file_id")
        expected_hash = payload.get("file_hash")
        content = payload.get("content")
        record = db.get(FileRecord, file_id)
        if record is None:
            raise ValueError(f"file record not found: {file_id}")
        if record.file_hash != expected_hash:
            raise ValueError(f"file hash lineage mismatch for file: {file_id}")
        if not isinstance(content, str) or not content:
            raise ValueError("file parse content is required")
        if hashlib.sha256(content.encode("utf-8")).hexdigest() != record.file_hash:
            raise ValueError(f"content hash mismatch for file: {file_id}")
        chunks = _split_text(content, settings.parse_chunk_chars)
        if not chunks:
            raise ValueError(f"file has no parseable text: {file_id}")
        db.query(DocumentChunk).filter(DocumentChunk.file_id == record.id).delete(synchronize_session=False)
        for index, chunk in enumerate(chunks):
            db.add(DocumentChunk(
                file_id=record.id,
                chunk_index=index,
                content=chunk,
                content_hash=hashlib.sha256(chunk.encode("utf-8")).hexdigest(),
                file_hash=record.file_hash,
            ))
        record.status = "parsed"
        embedding_key = f"file-embed:{record.id}:{record.file_hash}"
        if db.scalars(select(Task).where(Task.idempotency_key == embedding_key)).first() is None:
            db.add(Task(kind="file_embed", idempotency_key=embedding_key, payload_json=json.dumps({"file_id": record.id, "file_hash": record.file_hash}, ensure_ascii=False)))
        return {"execution": "file_parse", "file_id": record.id, "file_hash": record.file_hash, "chunks": len(chunks), "status": record.status, "embedding_task": embedding_key}
    if task.kind == "file_embed":
        file_id = payload.get("file_id")
        expected_hash = payload.get("file_hash")
        record = db.get(FileRecord, file_id)
        if record is None or record.file_hash != expected_hash:
            raise ValueError(f"file hash lineage mismatch for embedding: {file_id}")
        chunks = db.scalars(select(DocumentChunk).where(DocumentChunk.file_id == record.id, DocumentChunk.file_hash == expected_hash).order_by(DocumentChunk.chunk_index)).all()
        if not chunks:
            raise ValueError(f"no chunks available for embedding: {file_id}")
        providers = set()
        for chunk in chunks:
            result = embed_text(chunk.content)
            chunk.embedding = result.vector
            chunk.embedding_provider = result.provider
            chunk.embedding_status = result.status
            chunk.embedded_at = datetime.now(timezone.utc)
            providers.add(result.provider)
        return {"execution": "file_embed", "file_id": record.id, "file_hash": record.file_hash, "chunks": len(chunks), "providers": sorted(providers), "status": "degraded" if any(item != "ollama" for item in providers) else "ready"}
    if task.kind == "replica_repair":
        file_id = payload.get("file_id")
        expected_hash = payload.get("file_hash")
        target_node_id = payload.get("target_node_id")
        record = db.get(FileRecord, file_id)
        target = db.get(Node, target_node_id)
        if record is None:
            raise ValueError(f"file record not found: {file_id}")
        if record.file_hash != expected_hash:
            raise ValueError(f"file hash lineage mismatch for file: {file_id}")
        if target is None or not target.is_replica:
            raise ValueError(f"target node is not an enabled replica: {target_node_id}")
        command_key = f"replica-sync:{task.id}"
        command = db.scalars(select(RemoteCommand).where(RemoteCommand.idempotency_key == command_key)).first()
        if command is None:
            command = RemoteCommand(
                idempotency_key=command_key,
                node_id=target_node_id,
                command_type="sync_replica",
                payload_json=json.dumps({
                    "repair_task_id": task.id,
                    "file_id": record.id,
                    "file_hash": record.file_hash,
                    "relative_path": record.path,
                }, ensure_ascii=False),
                requested_by="replica-worker",
            )
            db.add(command)
            db.flush()
            db.add(AuditLog(actor="replica-worker", action="remote_command.queued", resource_type="remote_command", resource_id=str(command.id), detail=json.dumps({"repair_task_id": task.id}, ensure_ascii=False)))
        elif command.status == "failed":
            command.status = "queued"
            command.error = None
            command.result_json = None
            command.claimed_at = None
            db.add(AuditLog(actor="replica-worker", action="remote_command.requeued", resource_type="remote_command", resource_id=str(command.id), detail=json.dumps({"repair_task_id": task.id, "reason": "operator_retry"}, ensure_ascii=False)))
        task.status = "waiting"
        return {"execution": "replica_repair_dispatched", "command_id": command.id, "repair_task_id": task.id, "target_node_id": target_node_id, "status": "waiting"}
    if task.kind == "pipeline_run":
        from . import langgraph_flow  # local import keeps the optional package truly optional

        file_id = int(payload.get("file_id"))
        file_hash = payload.get("file_hash")
        content = payload.get("content")
        if not file_id or not file_hash:
            raise ValueError("pipeline_run requires file_id and file_hash")
        result = langgraph_flow.run_file_pipeline(file_id, file_hash, content)
        return {"execution": "pipeline_run", **result}
    if task.kind == "backup":
        from .backup import perform_backup

        result = perform_backup()
        return {"execution": "backup", "backup_id": result["backup_id"], "files": [item["name"] for item in result["files"]]}
    raise ValueError(f"task executor not installed for kind: {task.kind}")


def run_task_once(db: Session, worker_id: str, lease_seconds: int = 120) -> Task | None:
    task = claim_next_task(db, worker_id, lease_seconds)
    if not task:
        return None
    try:
        task.result_json = json.dumps(execute_task_payload(db, task), ensure_ascii=False)
        task.error = None
        task.status = "waiting" if task.kind == "replica_repair" else "success"
        task.claimed_at = None
        task.claimed_by = None
        _audit_task(db, task, "dispatched" if task.status == "waiting" else "success", worker_id)
    except Exception as exc:  # task failures must be persisted, then the worker continues
        task.status = "failed"
        task.error = str(exc)
        task.claimed_at = None
        task.claimed_by = None
        if task.kind == "file_parse":
            try:
                payload = json.loads(task.payload_json or "{}")
                record = db.get(FileRecord, payload.get("file_id"))
                if record is not None and record.file_hash == payload.get("file_hash"):
                    record.status = "parse_failed"
            except (TypeError, ValueError, json.JSONDecodeError):
                pass
        _audit_task(db, task, "failed", worker_id, {"error": str(exc)})
        _upsert_alert(db, fingerprint=f"task.failed:{task.id}", severity="high", kind="task_failed", resource_type="task", resource_id=str(task.id), message=f"任务 #{task.id} ({task.kind}) 执行失败：{task.error}")
    else:
        _resolve_alert(db, f"task.failed:{task.id}")
    db.commit()
    db.refresh(task)
    return task


def run_task_by_id(db: Session, task_id: int, worker_id: str, lease_seconds: int = 120) -> Task | None:
    task = claim_task_by_id(db, task_id, worker_id, lease_seconds)
    if not task:
        return None
    try:
        task.result_json = json.dumps(execute_task_payload(db, task), ensure_ascii=False)
        task.error = None
        task.status = "waiting" if task.kind == "replica_repair" else "success"
        task.claimed_at = None
        task.claimed_by = None
        _audit_task(db, task, "dispatched" if task.status == "waiting" else "success", worker_id, {"delivery": "celery"})
    except Exception as exc:
        task.status = "failed"
        task.error = str(exc)
        task.claimed_at = None
        task.claimed_by = None
        _audit_task(db, task, "failed", worker_id, {"error": str(exc), "delivery": "celery"})
        _upsert_alert(db, fingerprint=f"task.failed:{task.id}", severity="high", kind="task_failed", resource_type="task", resource_id=str(task.id), message=f"任务 #{task.id} ({task.kind}) 执行失败：{task.error}")
    else:
        _resolve_alert(db, f"task.failed:{task.id}")
    db.commit()
    db.refresh(task)
    return task


def create_dsh_review_proposal(db: Session, file_id: int) -> Proposal:
    """Run the DSH document-review adapter and persist the result as a pending proposal.

    The model output is never applied to knowledge data: it only becomes a
    ``document_review`` proposal that a named administrator must approve or reject.
    """
    record = db.get(FileRecord, file_id)
    if record is None:
        raise ValueError("file not found")
    chunks = db.scalars(
        select(DocumentChunk)
        .where(DocumentChunk.file_id == file_id, DocumentChunk.file_hash == record.file_hash)
        .order_by(DocumentChunk.chunk_index)
    ).all()
    if not chunks:
        raise ValueError("file has no parsed content to review")
    content = "\n\n".join(chunk.content for chunk in chunks)
    from . import dsh_client  # local import keeps the adapter optional

    result = dsh_client.review_document(record.path, content)
    title = f"文档审核：{record.path[:180]}"
    existing = db.scalars(
        select(Proposal).where(
            Proposal.kind == "document_review",
            Proposal.title == title,
            Proposal.status == "pending",
        ).order_by(Proposal.created_at.desc())
    ).first()
    if existing is not None:
        return existing
    body = json.dumps(
        {
            "source": result.provider,
            "dsh_status": result.status,
            "verdict": result.verdict,
            "confidence": result.confidence,
            "summary": result.summary,
            "human_review_required": True,
            "raw": result.raw,
        },
        ensure_ascii=False,
        default=str,
    )
    proposal = Proposal(kind="document_review", title=title, body=body, created_by="dsh-adapter")
    db.add(proposal)
    db.flush()
    db.add(AuditLog(actor="dsh-adapter", action="proposal.document_review_created", resource_type="proposal", resource_id=str(proposal.id)))
    db.commit()
    db.refresh(proposal)
    return proposal


def store_blob(db: Session, file_id: int, file_hash: str, data: bytes, mime_type: str | None = None) -> BlobObject:
    from .storage import _safe_key, get_storage, sha256_bytes

    record = db.get(FileRecord, file_id)
    if record is None:
        raise ValueError("file not found")
    if record.file_hash != file_hash:
        raise ValueError("file hash mismatch")
    if sha256_bytes(data) != file_hash:
        raise ValueError("content hash does not match file hash")
    existing = db.scalars(select(BlobObject).where(BlobObject.file_id == file_id, BlobObject.file_hash == file_hash)).first()
    if existing is not None:
        return existing
    storage = get_storage()
    key = _safe_key(file_hash)
    blob = BlobObject(
        file_id=file_id,
        file_hash=file_hash,
        mime_type=mime_type,
        size_bytes=len(data),
        storage_key=key,
        storage_backend=storage.name,
        sha256=file_hash,
    )
    # Reserve the DB row first; if the storage write fails, the uncommitted row
    # rolls back and no orphan object is left behind.
    db.add(blob)
    db.flush()
    storage.put(key, data)
    db.add(AuditLog(actor="node-adapter", action="blob.stored", resource_type="blob_object", resource_id=str(blob.id)))
    db.commit()
    db.refresh(blob)
    return blob


def load_blob(db: Session, file_id: int) -> tuple[BlobObject, bytes]:
    from .storage import get_storage_for

    record = db.get(FileRecord, file_id)
    if record is None or not record.alive or record.status == "superseded":
        raise ValueError("current file version not found")
    blob = db.scalars(
        select(BlobObject).where(BlobObject.file_id == file_id, BlobObject.file_hash == record.file_hash)
    ).first()
    if blob is None:
        raise ValueError("blob not found")
    return blob, get_storage_for(blob.storage_backend).get(blob.storage_key)


def review_proposal(db: Session, proposal: Proposal, decision: str, reviewer: str) -> Proposal:
    if proposal.status != "pending":
        raise ValueError("proposal already reviewed")
    proposal.status = decision
    proposal.reviewed_by = reviewer
    proposal.reviewed_at = datetime.now(timezone.utc)
    db.add(AuditLog(actor=reviewer, action=f"proposal.{decision}", resource_type="proposal", resource_id=str(proposal.id)))
    db.commit()
    db.refresh(proposal)
    return proposal


def create_remote_command(db: Session, data) -> RemoteCommand:
    existing = db.scalars(select(RemoteCommand).where(RemoteCommand.idempotency_key == data.idempotency_key)).first()
    if existing:
        return existing
    if not db.get(Node, data.node_id):
        raise ValueError("node not found")
    command = RemoteCommand(
        idempotency_key=data.idempotency_key,
        node_id=data.node_id,
        command_type=data.command_type,
        payload_json=json.dumps(data.payload, ensure_ascii=False),
        requested_by=data.requested_by,
    )
    db.add(command)
    db.flush()
    db.add(AuditLog(actor=data.requested_by, action="remote_command.queued", resource_type="remote_command", resource_id=str(command.id)))
    db.commit()
    db.refresh(command)
    return command


def acknowledge_remote_command(db: Session, command: RemoteCommand, data) -> RemoteCommand:
    if command.status not in {"queued", "running"}:
        raise ValueError("command already completed")
    command.status = data.status
    command.result_json = json.dumps(data.result, ensure_ascii=False)
    command.error = data.error
    command.claimed_at = datetime.now(timezone.utc) if data.status == "running" else None
    if command.command_type == "sync_replica":
        payload = json.loads(command.payload_json or "{}")
        repair_task = db.get(Task, payload.get("repair_task_id"))
        record = db.get(FileRecord, payload.get("file_id"))
        if repair_task is None:
            raise ValueError("replica repair task not found")
        if data.status == "success":
            verified = data.result.get("verified") is True
            returned_hash = data.result.get("file_hash")
            if not verified or returned_hash != payload.get("file_hash"):
                command.status = "failed"
                command.error = "sync ACK must include verified=true and matching file_hash"
                repair_task.status = "failed"
                repair_task.error = command.error
            else:
                if record is None or record.file_hash != payload.get("file_hash"):
                    command.status = "failed"
                    command.error = "replica repair file lineage changed"
                    repair_task.status = "failed"
                    repair_task.error = command.error
                else:
                    replica = db.scalars(select(FileReplica).where(FileReplica.file_id == record.id, FileReplica.node_id == command.node_id)).first()
                    now = datetime.now(timezone.utc)
                    if replica is None:
                        replica = FileReplica(file_id=record.id, node_id=command.node_id, file_hash=record.file_hash, status="healthy", last_seen_at=now)
                        db.add(replica)
                    else:
                        replica.file_hash = record.file_hash
                        replica.status = "healthy"
                        replica.last_seen_at = now
                    repair_task.status = "success"
                    repair_task.error = None
        elif data.status == "failed":
            repair_task.status = "failed"
            repair_task.error = data.error or "replica sync command failed"
        repair_task.claimed_at = None
        repair_task.claimed_by = None
    db.add(AuditLog(
        actor=command.node_id,
        action=f"remote_command.{command.status}",
        resource_type="remote_command",
        resource_id=str(command.id),
        detail=json.dumps({"error": command.error} if command.error else {}, ensure_ascii=False),
    ))
    db.commit()
    db.refresh(command)
    return command


def claim_next_remote_command(db: Session, node_id: str, lease_seconds: int = 120) -> RemoteCommand | None:
    """Claim one queued command for an edge node.

    PostgreSQL workers use row locking to prevent duplicate delivery. SQLite
    ignores `FOR UPDATE`, so local development remains single-worker only.
    """
    now = datetime.now(timezone.utc)
    lease_deadline = now - timedelta(seconds=lease_seconds)
    stale = db.scalars(
        select(RemoteCommand).where(
            RemoteCommand.node_id == node_id,
            RemoteCommand.status == "running",
            RemoteCommand.claimed_at.is_not(None),
            RemoteCommand.claimed_at < lease_deadline,
        )
    ).all()
    for command in stale:
        command.status = "queued"
        command.claimed_at = None
        command.error = "claim lease expired; command requeued"
        db.add(AuditLog(
            actor="system",
            action="remote_command.requeued",
            resource_type="remote_command",
            resource_id=str(command.id),
            detail=json.dumps({"reason": "lease_expired"}),
        ))
    if stale:
        db.flush()

    statement = (
        select(RemoteCommand)
        .where(RemoteCommand.node_id == node_id, RemoteCommand.status == "queued")
        .order_by(RemoteCommand.created_at, RemoteCommand.id)
        .with_for_update(skip_locked=True)
    )
    command = db.scalars(statement).first()
    if not command:
        return None
    command.status = "running"
    command.claimed_at = now
    db.add(AuditLog(
        actor=node_id,
        action="remote_command.claimed",
        resource_type="remote_command",
        resource_id=str(command.id),
    ))
    db.commit()
    db.refresh(command)
    return command
