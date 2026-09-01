from pathlib import Path
import json
from contextlib import asynccontextmanager
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from .config import settings
from .db import get_db, initialize_database
from .models import Node, FileRecord, FileReplica, DocumentChunk, BlobObject, Task, Proposal, RemoteCommand, Alert, RetrievalLog, GapItem
from .schemas import NodeHeartbeat, NodeStatusEvent, FileReport, FileContentUpload, TaskCreate, ProposalCreate, ProposalReview, MobileCommandCreate, MobileCommandAck, KnowledgeSearchRequest, PipelineRunRequest, ApiKeyCreate, TokenCreate, RagChatRequest, GapStatusUpdate, ConfigUpdate
from .services import heartbeat, apply_node_status_event, report_file, queue_file_content_parse, create_task, transition_task, review_proposal, refresh_node_statuses, reconcile_file_liveness, replica_policy_health, create_remote_command, acknowledge_remote_command, claim_next_remote_command, acknowledge_alert, search_knowledge, create_dsh_review_proposal, store_blob, load_blob, list_gap_items, run_gap_summary, update_gap_item_status
from .dispatcher import executor_health
from .security import authorize, create_api_key, revoke_api_key, create_token, oidc_status
from . import dify_client
from .settings_store import list_overrides, set_override, get_effective_setting, mask_secret, WRITABLE_KEYS

@asynccontextmanager
async def lifespan(_app: FastAPI):
    initialize_database()
    yield


app = FastAPI(title="Knowledge Hub", version="0.1.0", lifespan=lifespan)


def require_admin(request: Request, x_admin_key: str | None = Header(default=None), db: Session = Depends(get_db)) -> None:
    if settings.app_env != "production":
        return
    if settings.admin_api_key and x_admin_key == settings.admin_api_key:
        return
    if authorize(request, db, "admin"):
        return
    raise HTTPException(401, "admin authentication required")


def require_node(request: Request, x_node_key: str | None = Header(default=None), db: Session = Depends(get_db)) -> None:
    if settings.app_env != "production":
        return
    if settings.node_api_key and x_node_key == settings.node_api_key:
        return
    if authorize(request, db, "node"):
        return
    raise HTTPException(401, "node authentication required")


def require_admin_or_node(request: Request, x_admin_key: str | None = Header(default=None), x_node_key: str | None = Header(default=None), db: Session = Depends(get_db)) -> None:
    """允许管理员或节点（中心控制台用 admin，边缘端用 node key）触发数据同步。"""
    if settings.app_env != "production":
        return
    if settings.admin_api_key and x_admin_key == settings.admin_api_key:
        return
    if settings.node_api_key and x_node_key == settings.node_api_key:
        return
    if authorize(request, db, "admin") or authorize(request, db, "node"):
        return
    raise HTTPException(401, "admin or node authentication required")


def require_mobile(request: Request, x_mobile_key: str | None = Header(default=None), db: Session = Depends(get_db)) -> None:
    if settings.app_env != "production":
        return
    if settings.mobile_api_key and x_mobile_key == settings.mobile_api_key:
        return
    if authorize(request, db, "mobile"):
        return
    raise HTTPException(401, "mobile authentication required")


def require_mqtt_bridge(request: Request, x_bridge_key: str | None = Header(default=None), db: Session = Depends(get_db)) -> None:
    # Note: unlike the other require_* guards this one has NO development-mode
    # bypass. The bridge writes node status, so it must always authenticate;
    # configure MQTT_BRIDGE_API_KEY even for local development.
    if settings.mqtt_bridge_api_key and x_bridge_key == settings.mqtt_bridge_api_key:
        return
    if authorize(request, db, "mqtt_bridge"):
        return
    raise HTTPException(401, "MQTT bridge authentication required")


def require_search(request: Request, x_search_key: str | None = Header(default=None), db: Session = Depends(get_db)) -> None:
    if settings.app_env != "production":
        return
    if settings.search_api_key and x_search_key == settings.search_api_key:
        return
    if authorize(request, db, "search"):
        return
    raise HTTPException(401, "search authentication required")


def require_download(request: Request, x_download_key: str | None = Header(default=None), db: Session = Depends(get_db)) -> None:
    """Protect the standalone download gateway independently from admin/search keys."""
    if settings.app_env != "production":
        return
    if settings.download_api_key and x_download_key == settings.download_api_key:
        return
    if authorize(request, db, "download"):
        return
    raise HTTPException(401, "download authentication required")


@app.get("/healthz")
def healthz(db: Session = Depends(get_db)):
    db.execute(select(1))
    return {"status": "ok", "environment": settings.app_env, "executor": executor_health()}


@app.get("/api/v1/executor/health", dependencies=[Depends(require_admin)])
def executor_status():
    return executor_health()


@app.get("/api/v1/orchestration/status", dependencies=[Depends(require_admin)])
def orchestration_status():
    from .langgraph_flow import orchestration_status as _status

    return _status()


@app.post("/api/v1/pipeline/run", dependencies=[Depends(require_admin)])
def pipeline_run(data: PipelineRunRequest, db: Session = Depends(get_db)):
    payload = {"file_id": data.file_id, "file_hash": data.file_hash, "content": data.content}
    task = create_task(db, type("PipelineTask", (), {"kind": "pipeline_run", "idempotency_key": data.idempotency_key, "payload": payload})())
    return {"task_id": task.id, "status": task.status, "execution": "pipeline_run_queued"}


@app.get("/", response_class=HTMLResponse)
def console():
    return (Path(__file__).parent / "console.html").read_text(encoding="utf-8")


@app.post("/api/v1/nodes/heartbeat", dependencies=[Depends(require_node)])
def node_heartbeat(data: NodeHeartbeat, db: Session = Depends(get_db)):
    return heartbeat(db, data)


@app.post("/internal/v1/mqtt/node-status", dependencies=[Depends(require_mqtt_bridge)])
def mqtt_node_status(data: NodeStatusEvent, db: Session = Depends(get_db)):
    return apply_node_status_event(db, data)


@app.get("/api/v1/nodes", dependencies=[Depends(require_admin)])
def list_nodes(db: Session = Depends(get_db)):
    refresh_node_statuses(db, settings.node_offline_after_seconds)
    return db.scalars(select(Node).order_by(Node.status.desc(), Node.hostname)).all()


@app.post("/api/v1/files/report", dependencies=[Depends(require_node)])
def file_report(data: FileReport, db: Session = Depends(get_db)):
    return report_file(db, data)


@app.post("/api/v1/files/{file_id}/content", dependencies=[Depends(require_node)])
def file_content(file_id: int, data: FileContentUpload, db: Session = Depends(get_db)):
    if not db.get(FileRecord, file_id):
        raise HTTPException(404, "file not found")
    if len(data.content.encode("utf-8")) > settings.max_upload_bytes:
        raise HTTPException(413, "file content exceeds MAX_UPLOAD_BYTES")
    try:
        task = queue_file_content_parse(db, file_id, data.source_node_id, data.file_hash, data.content)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"file_id": file_id, "task_id": task.id, "status": task.status, "execution": "deterministic_text_parse"}


@app.get("/api/v1/files/summary", dependencies=[Depends(require_admin)])
def file_summary(db: Session = Depends(get_db)):
    total = db.scalar(select(func.count(FileRecord.id))) or 0
    missing = db.scalar(select(func.count(FileRecord.id)).where(FileRecord.status == "missing")) or 0
    return {"total": total, "missing": missing, "healthy_rate": round((total - missing) / total, 4) if total else 1}


@app.get("/api/v1/categories")
def category_list(db: Session = Depends(get_db)):
    """Host-defined knowledge partitions merged with partitions already present in the index."""
    configured = [c.strip() for c in settings.knowledge_categories.split(",") if c.strip()]
    rows = db.execute(
        select(FileRecord.category, func.count(FileRecord.id))
        .where(FileRecord.alive.is_(True))
        .group_by(FileRecord.category)
    ).all()
    counts = {cat or "未分类": n for cat, n in rows}
    merged = list(dict.fromkeys([*configured, *counts.keys()]))
    return {"categories": merged, "counts": counts, "default": configured[0] if configured else "未分类"}


@app.post("/api/v1/reconciliation/files", dependencies=[Depends(require_admin)])
def reconcile_files(db: Session = Depends(get_db)):
    return reconcile_file_liveness(db, settings.file_missing_after_seconds, settings.fixed_replica_node_ids)


@app.get("/api/v1/replicas", dependencies=[Depends(require_admin)])
def replica_summary(db: Session = Depends(get_db)):
    rows = db.scalars(select(FileReplica).order_by(FileReplica.updated_at.desc(), FileReplica.id.desc()).limit(500)).all()
    return rows


@app.get("/api/v1/replica-policy/health", dependencies=[Depends(require_admin)])
def replica_policy_status(node_ids: str | None = None, db: Session = Depends(get_db)):
    configured = [item.strip() for item in (node_ids if node_ids is not None else settings.replica_node_ids).split(",") if item.strip()]
    return replica_policy_health(db, configured)


@app.get("/api/v1/replica-repairs", dependencies=[Depends(require_admin)])
def replica_repairs(db: Session = Depends(get_db)):
    return db.scalars(select(Task).where(Task.kind == "replica_repair").order_by(Task.updated_at.desc(), Task.id.desc()).limit(200)).all()


@app.post("/api/v1/replica-repairs/{task_id}/retry", dependencies=[Depends(require_admin)])
def replica_repair_retry(task_id: int, db: Session = Depends(get_db)):
    task = db.get(Task, task_id)
    if not task or task.kind != "replica_repair":
        raise HTTPException(404, "replica repair task not found")
    try:
        return transition_task(db, task, "pending")
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.get("/api/v1/files/{file_id}/replicas", dependencies=[Depends(require_admin)])
def file_replicas(file_id: int, db: Session = Depends(get_db)):
    if not db.get(FileRecord, file_id):
        raise HTTPException(404, "file not found")
    return db.scalars(select(FileReplica).where(FileReplica.file_id == file_id).order_by(FileReplica.status, FileReplica.node_id)).all()


@app.get("/api/v1/nodes/{node_id}/files", dependencies=[Depends(require_admin)])
def node_files(node_id: str, db: Session = Depends(get_db)):
    if not db.get(Node, node_id):
        raise HTTPException(404, "node not found")
    return db.scalars(
        select(FileReplica).where(FileReplica.node_id == node_id).order_by(FileReplica.updated_at.desc(), FileReplica.id.desc())
    ).all()


@app.get("/api/v1/pipeline/files", dependencies=[Depends(require_admin)])
def pipeline_files(db: Session = Depends(get_db)):
    """Return recent file records with registration and parse task states."""
    files = db.scalars(select(FileRecord).order_by(FileRecord.updated_at.desc(), FileRecord.id.desc()).limit(200)).all()
    output = []
    for record in files:
        register_task = db.scalars(
            select(Task)
            .where(Task.kind == "file_register", Task.idempotency_key == f"file-register:{record.id}:{record.file_hash}")
        ).first()
        parse_task = db.scalars(
            select(Task)
            .where(Task.kind == "file_parse", Task.idempotency_key == f"file-parse:{record.id}:{record.file_hash}")
        ).first()
        embed_task = db.scalars(
            select(Task).where(Task.kind == "file_embed", Task.idempotency_key == f"file-embed:{record.id}:{record.file_hash}")
        ).first()
        active_task = parse_task or register_task
        chunk_count = db.scalar(select(func.count(DocumentChunk.id)).where(DocumentChunk.file_id == record.id)) or 0
        output.append({
            "id": record.id,
            "path": record.path,
            "category": record.category,
            "file_hash": record.file_hash,
            "size_bytes": record.size_bytes,
            "status": record.status,
            "version": record.version,
            "source_node_id": record.source_node_id,
            "alive": record.alive,
            "last_seen_at": record.last_seen_at,
            "task_id": active_task.id if active_task else None,
            "task_kind": active_task.kind if active_task else None,
            "task_status": active_task.status if active_task else None,
            "task_error": active_task.error if active_task else None,
            "register_task_id": register_task.id if register_task else None,
            "register_task_status": register_task.status if register_task else None,
            "parse_task_id": parse_task.id if parse_task else None,
            "parse_task_status": parse_task.status if parse_task else None,
            "embed_task_id": embed_task.id if embed_task else None,
            "embed_task_status": embed_task.status if embed_task else None,
            "embedded_chunk_count": db.scalar(select(func.count(DocumentChunk.id)).where(DocumentChunk.file_id == record.id, DocumentChunk.embedding.is_not(None))) or 0,
            "embedding_status": db.scalars(select(DocumentChunk.embedding_status).where(DocumentChunk.file_id == record.id).order_by(DocumentChunk.chunk_index)).first(),
            "embedding_provider": db.scalars(select(DocumentChunk.embedding_provider).where(DocumentChunk.file_id == record.id).order_by(DocumentChunk.chunk_index)).first(),
            "chunk_count": chunk_count,
            "replica_count": db.scalar(select(func.count(FileReplica.id)).where(FileReplica.file_id == record.id, FileReplica.status == "healthy")) or 0,
            "updated_at": record.updated_at,
        })
    return output


@app.get("/api/v1/files/{file_id}/chunks", dependencies=[Depends(require_admin)])
def file_chunks(file_id: int, db: Session = Depends(get_db)):
    if not db.get(FileRecord, file_id):
        raise HTTPException(404, "file not found")
    return db.scalars(select(DocumentChunk).where(DocumentChunk.file_id == file_id).order_by(DocumentChunk.chunk_index)).all()


@app.get("/api/v1/gateway/files/{file_id}", response_class=PlainTextResponse, dependencies=[Depends(require_download)])
def gateway_file_download(file_id: int, db: Session = Depends(get_db)):
    """Serve the assembled current text version without modifying Dify or exposing stale files."""
    record = db.get(FileRecord, file_id)
    if record is None or not record.alive or record.status == "superseded":
        raise HTTPException(404, "current file version not found")
    chunks = db.scalars(
        select(DocumentChunk)
        .where(DocumentChunk.file_id == record.id, DocumentChunk.file_hash == record.file_hash)
        .order_by(DocumentChunk.chunk_index)
    ).all()
    if not chunks:
        raise HTTPException(409, "file content is not available yet")
    content = "\n\n".join(chunk.content for chunk in chunks)
    return PlainTextResponse(
        content,
        headers={
            "ETag": f'"{record.file_hash}"',
            "X-File-Hash": record.file_hash,
            "X-File-Version": str(record.version),
            "X-File-Id": str(record.id),
        },
    )


@app.post("/api/v1/files/{file_id}/blob", dependencies=[Depends(require_node)])
async def file_blob_upload(
    file_id: int,
    request: Request,
    x_file_hash: str = Header(...),
    x_node_id: str = Header(...),
    content_type: str | None = Header(default=None),
    db: Session = Depends(get_db),
):
    """Store opaque binary bytes for a reported file, validated against its SHA-256."""
    data = await request.body()
    if len(data) > settings.max_upload_bytes:
        raise HTTPException(413, "blob exceeds MAX_UPLOAD_BYTES")
    try:
        blob = store_blob(db, file_id, x_file_hash, data, content_type)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"file_id": file_id, "blob_id": blob.id, "size_bytes": blob.size_bytes, "storage_backend": blob.storage_backend, "sha256": blob.sha256}


@app.get("/api/v1/gateway/files/{file_id}/binary", dependencies=[Depends(require_download)])
def gateway_file_binary(file_id: int, db: Session = Depends(get_db)):
    """Serve the current binary version through the separately keyed download gateway."""
    try:
        blob, data = load_blob(db, file_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    return Response(
        content=data,
        media_type=blob.mime_type or "application/octet-stream",
        headers={
            "ETag": f'"{blob.file_hash}"',
            "X-File-Hash": blob.file_hash,
            "X-File-Id": str(file_id),
            "X-Storage-Backend": blob.storage_backend,
        },
    )


@app.get("/api/v1/blob-objects", dependencies=[Depends(require_admin)])
def blob_object_list(db: Session = Depends(get_db)):
    rows = db.scalars(select(BlobObject).order_by(BlobObject.created_at.desc(), BlobObject.id.desc()).limit(500)).all()
    return rows


@app.post("/api/v1/knowledge/search", dependencies=[Depends(require_search)])
def knowledge_search(data: KnowledgeSearchRequest, x_actor: str | None = Header(default=None), db: Session = Depends(get_db)):
    if len(data.query) > settings.max_search_query_chars:
        raise HTTPException(413, "query exceeds MAX_SEARCH_QUERY_CHARS")
    top_k = min(data.top_k, settings.max_search_top_k)
    actor = "search-gateway" if settings.app_env == "production" else (x_actor or "search-gateway")
    try:
        result = search_knowledge(db, data.query, top_k=top_k, idempotency_key=data.idempotency_key, actor=actor, category=data.category)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return result


@app.post("/api/v1/tasks", dependencies=[Depends(require_admin)])
def task_create(data: TaskCreate, db: Session = Depends(get_db)):
    return create_task(db, data)


@app.get("/api/v1/tasks", dependencies=[Depends(require_admin)])
def task_list(db: Session = Depends(get_db)):
    return db.scalars(select(Task).order_by(Task.created_at.desc()).limit(100)).all()


@app.get("/api/v1/alert-deliveries", dependencies=[Depends(require_admin)])
def alert_delivery_list(db: Session = Depends(get_db)):
    from .alerting import alert_delivery_summary
    from .models import AlertDelivery

    summary = alert_delivery_summary(db)
    rows = db.scalars(select(AlertDelivery).order_by(AlertDelivery.created_at.desc(), AlertDelivery.id.desc()).limit(500)).all()
    return {"summary": summary, "deliveries": rows}


@app.post("/api/v1/alert-deliveries/process", dependencies=[Depends(require_admin)])
def alert_delivery_process(db: Session = Depends(get_db)):
    from .alerting import process_alert_deliveries

    return process_alert_deliveries(db)


@app.get("/api/v1/alerts", dependencies=[Depends(require_admin)])
def alert_list(status: str | None = None, db: Session = Depends(get_db)):
    query = select(Alert).order_by(Alert.status, Alert.last_seen_at.desc(), Alert.id.desc()).limit(500)
    if status:
        query = query.where(Alert.status == status)
    return db.scalars(query).all()


@app.post("/api/v1/rag/chat", dependencies=[Depends(require_search)])
def rag_chat(data: RagChatRequest, db: Session = Depends(get_db)):
    """产品问答：代理 Dify。严格接地（无参考命中时拒绝回答），返回带参考链接的结果。"""
    return dify_client.chat_with_references(db, data.query, top_k=data.top_k)


@app.post("/api/v1/rag/ingest", dependencies=[Depends(require_admin_or_node)])
def rag_ingest(file_id: int, db: Session = Depends(get_db)):
    """把文件接入 Dify 数据集（中心控制台/边缘端均可触发）。

    已解析 → 立即接入；尚未解析完 → 排队 dify_ingest 任务，解析后由 worker 自动接入。
    """
    record = db.get(FileRecord, file_id)
    if record is None:
        raise HTTPException(404, "file not found")
    chunk_count = db.scalar(select(func.count(DocumentChunk.id)).where(DocumentChunk.file_id == record.id, DocumentChunk.file_hash == record.file_hash)) or 0
    if chunk_count == 0:
        key = f"dify-ingest:{record.id}:{record.file_hash}"
        task = db.scalars(select(Task).where(Task.idempotency_key == key)).first()
        if task is None:
            task = create_task(db, type("IngestTask", (), {"kind": "dify_ingest", "idempotency_key": key, "payload": {"file_id": record.id}}))
        return {"queued": True, "file_id": record.id, "task_id": task.id, "message": "文件尚未解析完成，已排队，解析后自动同步到 Dify 数据集"}
    try:
        return dify_client.ingest_file_to_dify(db, file_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/v1/dify/status", dependencies=[Depends(require_admin)])
def dify_status(db: Session = Depends(get_db)):
    return dify_client.dify_status(db)


@app.get("/api/v1/gaps", dependencies=[Depends(require_admin)])
def gap_list(status: str | None = None, limit: int = 200, db: Session = Depends(get_db)):
    """待添加清单：常问但知识库未命中参考的问题。"""
    return list_gap_items(db, status=status, limit=max(1, min(limit, 500)))


@app.post("/api/v1/gaps/summarize", dependencies=[Depends(require_admin)])
def gap_summarize(db: Session = Depends(get_db)):
    """立即执行一次汇总（把统计窗口内未命中的检索聚合成清单项）。"""
    window = float(get_effective_setting(db, "gap_summary_interval_hours", settings.gap_summary_interval_hours) or 24)
    updated = run_gap_summary(db, window_hours=window)
    return {"updated": updated}


@app.patch("/api/v1/gaps/{gap_id}", dependencies=[Depends(require_admin)])
def gap_update(gap_id: int, data: GapStatusUpdate, db: Session = Depends(get_db)):
    try:
        return update_gap_item_status(db, gap_id, data.status, data.note)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.get("/api/v1/admin/config", dependencies=[Depends(require_admin)])
def config_get(db: Session = Depends(get_db)):
    """后台配置总控台：全部可写键的当前生效值（敏感键掩码），以及已有覆盖。"""
    effective: dict = {}
    for key, kind in WRITABLE_KEYS.items():
        value = get_effective_setting(db, key, getattr(settings, key, None))
        if kind == "secret" and value:
            effective[key] = mask_secret(str(value))
        else:
            effective[key] = value
    return {"effective": effective, "overrides": list_overrides(db), "writable": sorted(WRITABLE_KEYS)}


@app.post("/api/v1/admin/config", dependencies=[Depends(require_admin)])
def config_set(data: ConfigUpdate, db: Session = Depends(get_db)):
    try:
        set_override(db, data.key, data.value)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True, "key": data.key}


@app.post("/api/v1/alerts/{alert_id}/ack", dependencies=[Depends(require_admin)])
def alert_ack(alert_id: int, db: Session = Depends(get_db)):
    alert = db.get(Alert, alert_id)
    if not alert:
        raise HTTPException(404, "alert not found")
    try:
        return acknowledge_alert(db, alert, "admin", "acknowledged")
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post("/api/v1/alerts/{alert_id}/resolve", dependencies=[Depends(require_admin)])
def alert_resolve(alert_id: int, db: Session = Depends(get_db)):
    alert = db.get(Alert, alert_id)
    if not alert:
        raise HTTPException(404, "alert not found")
    try:
        return acknowledge_alert(db, alert, "admin", "resolved")
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post("/api/v1/tasks/{task_id}/retry", dependencies=[Depends(require_admin)])
def task_retry(task_id: int, db: Session = Depends(get_db)):
    task = db.get(Task, task_id)
    if not task:
        raise HTTPException(404, "task not found")
    try:
        return transition_task(db, task, "pending")
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post("/api/v1/proposals")
def proposal_create(data: ProposalCreate, db: Session = Depends(get_db)):
    proposal = Proposal(kind=data.kind, title=data.title, body=data.body, created_by=data.created_by)
    db.add(proposal)
    db.commit()
    db.refresh(proposal)
    return proposal


@app.post("/api/v1/admin/keys", dependencies=[Depends(require_admin)])
def admin_key_create(data: ApiKeyCreate, db: Session = Depends(get_db)):
    from .models import ApiKey

    try:
        key, plaintext = create_api_key(db, data.role, data.label, data.ttl_seconds)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"key_id": key.key_id, "role": key.role, "label": key.label, "active": key.active, "expires_at": key.expires_at, "api_key": plaintext, "note": "Store this secret now; it is shown only once."}


@app.get("/api/v1/admin/keys", dependencies=[Depends(require_admin)])
def admin_key_list(db: Session = Depends(get_db)):
    from .models import ApiKey

    keys = db.scalars(select(ApiKey).order_by(ApiKey.created_at.desc())).all()
    return [{"id": key.id, "key_id": key.key_id, "role": key.role, "label": key.label, "active": key.active, "created_at": key.created_at, "last_used_at": key.last_used_at, "expires_at": key.expires_at} for key in keys]


@app.post("/api/v1/admin/keys/{key_id}/revoke", dependencies=[Depends(require_admin)])
def admin_key_revoke(key_id: str, db: Session = Depends(get_db)):
    try:
        key = revoke_api_key(db, key_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    return {"key_id": key.key_id, "active": key.active}


@app.post("/api/v1/admin/tokens", dependencies=[Depends(require_admin)])
def admin_token_create(data: TokenCreate):
    token = create_token(data.principal, data.roles, data.ttl_seconds)
    return {"token": token, "principal": data.principal, "roles": data.roles}


@app.get("/api/v1/security/status", dependencies=[Depends(require_admin)])
def security_status():
    return oidc_status()


@app.post("/api/v1/admin/backup", dependencies=[Depends(require_admin)])
def admin_backup():
    from .backup import perform_backup

    try:
        return perform_backup()
    except Exception as exc:  # surface operational failures as a clear 500 detail
        raise HTTPException(500, f"backup failed: {exc}") from exc


@app.get("/api/v1/admin/backup/status", dependencies=[Depends(require_admin)])
def admin_backup_status():
    from .backup import backup_status

    return backup_status()


@app.get("/api/v1/dsh/status", dependencies=[Depends(require_admin)])
def dsh_status():
    from . import dsh_client

    return dsh_client.status()


@app.post("/api/v1/dsh/review", dependencies=[Depends(require_admin)])
def dsh_review(file_id: int, db: Session = Depends(get_db)):
    try:
        proposal = create_dsh_review_proposal(db, file_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"proposal_id": proposal.id, "status": proposal.status, "kind": proposal.kind}


@app.get("/api/v1/proposals", dependencies=[Depends(require_admin)])
def proposal_list(db: Session = Depends(get_db)):
    return db.scalars(select(Proposal).order_by(Proposal.created_at.desc())).all()


@app.post("/api/v1/proposals/{proposal_id}/review", dependencies=[Depends(require_admin)])
def proposal_review(proposal_id: int, data: ProposalReview, db: Session = Depends(get_db)):
    proposal = db.get(Proposal, proposal_id)
    if not proposal:
        raise HTTPException(404, "proposal not found")
    try:
        return review_proposal(db, proposal, data.decision, data.reviewer)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.get("/api/v1/mobile/overview", dependencies=[Depends(require_mobile)])
def mobile_overview(db: Session = Depends(get_db)):
    refresh_node_statuses(db, settings.node_offline_after_seconds)
    nodes = db.scalars(select(Node)).all()
    total_files = db.scalar(select(func.count(FileRecord.id))) or 0
    missing_files = db.scalar(select(func.count(FileRecord.id)).where(FileRecord.status == "missing")) or 0
    pending_commands = db.scalar(select(func.count(RemoteCommand.id)).where(RemoteCommand.status.in_(["queued", "running"]))) or 0
    return {
        "nodes": {"total": len(nodes), "online": sum(n.status == "online" for n in nodes)},
        "files": {"total": total_files, "missing": missing_files},
        "pending_remote_commands": pending_commands,
        "capabilities": ["view_nodes", "view_files", "queue_remote_command", "view_command_status"],
    }


@app.get("/api/v1/mobile/nodes", dependencies=[Depends(require_mobile)])
def mobile_nodes(db: Session = Depends(get_db)):
    refresh_node_statuses(db, settings.node_offline_after_seconds)
    return db.scalars(select(Node).order_by(Node.status.desc(), Node.hostname)).all()


@app.get("/api/v1/mobile/alerts", dependencies=[Depends(require_mobile)])
def mobile_alerts(db: Session = Depends(get_db)):
    return db.scalars(select(Alert).where(Alert.status.in_(["open", "acknowledged"])).order_by(Alert.last_seen_at.desc()).limit(100)).all()


@app.post("/api/v1/mobile/commands", dependencies=[Depends(require_mobile)])
def mobile_command_create(data: MobileCommandCreate, db: Session = Depends(get_db)):
    try:
        command = create_remote_command(db, data)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    return {"id": command.id, "status": command.status, "execution_mode": "adapter_pending", "command_type": command.command_type, "node_id": command.node_id}


@app.get("/api/v1/mobile/commands/{command_id}", dependencies=[Depends(require_mobile)])
def mobile_command_status(command_id: int, db: Session = Depends(get_db)):
    command = db.get(RemoteCommand, command_id)
    if not command:
        raise HTTPException(404, "remote command not found")
    return command


@app.post("/api/v1/nodes/{node_id}/commands/{command_id}/ack", dependencies=[Depends(require_node)])
def node_command_ack(node_id: str, command_id: int, data: MobileCommandAck, db: Session = Depends(get_db)):
    command = db.get(RemoteCommand, command_id)
    if not command or command.node_id != node_id:
        raise HTTPException(404, "remote command not found")
    try:
        return acknowledge_remote_command(db, command, data)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.get("/api/v1/nodes/{node_id}/commands/next", dependencies=[Depends(require_node)])
def node_command_next(node_id: str, db: Session = Depends(get_db)):
    if not db.get(Node, node_id):
        raise HTTPException(404, "node not found")
    command = claim_next_remote_command(db, node_id, settings.remote_command_lease_seconds)
    if not command:
        return {"command": None}
    return {
        "command": {
            "id": command.id,
            "node_id": command.node_id,
            "command_type": command.command_type,
            "payload": json.loads(command.payload_json),
            "status": command.status,
        }
    }
