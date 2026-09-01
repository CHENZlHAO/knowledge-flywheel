"""Dify 问答/检索适配器。

产品问答链路：中心端代理 Dify chat-messages，默认严格接地（未命中参考时拒绝回答，
不返回模型自由发挥的答案），并把 Dify 引用（retriever_resources）映射为知识飞轮的
参考链接（下载网关）。同时把 Dify 检索写入检索日志，命中文件的片段计入热度加权。
"""
from urllib.parse import urljoin

import httpx
from sqlalchemy import select, update

from .config import settings
from .models import FileRecord, DocumentChunk
from .services import record_retrieval
from .settings_store import get_effective_setting

REFUSAL_ANSWER = "知识库中暂无相关内容，无法回答该问题。"


def _config(db) -> dict:
    return {
        "base_url": (get_effective_setting(db, "dify_base_url", settings.dify_base_url) or "").rstrip("/"),
        "api_key": get_effective_setting(db, "dify_api_key", settings.dify_api_key) or "",
        "dataset_id": get_effective_setting(db, "dify_dataset_id", settings.dify_dataset_id) or "",
        "strict": bool(get_effective_setting(db, "rag_strict", settings.rag_strict)),
        "min_score": float(get_effective_setting(db, "rag_min_score", settings.rag_min_score) or 0),
    }


def _headers(cfg: dict) -> dict:
    return {"Authorization": f"Bearer {cfg['api_key']}", "Content-Type": "application/json"}


def dify_status(db) -> dict:
    cfg = _config(db)
    if not cfg["base_url"] or not cfg["api_key"]:
        return {"configured": False, "reachable": False, "message": "Dify 未配置：请在后台配置总控台填写 base_url 与 api_key"}
    try:
        r = httpx.get(urljoin(cfg["base_url"], "/v1/apps"), headers=_headers(cfg), timeout=5)
        if r.status_code < 300:
            return {"configured": True, "reachable": True, "status": r.status_code, "message": "Dify API 可达"}
        return {"configured": True, "reachable": True, "status": r.status_code, "message": f"Dify 可达但返回 HTTP {r.status_code}（请确认密钥是应用 API 密钥）"}
    except Exception as exc:  # noqa: BLE001
        return {"configured": True, "reachable": False, "status": None, "message": f"无法连接 Dify：{exc}"}


def _resolve_reference(db, document_name: str, content: str) -> dict:
    """把 Dify 文档名（约定为知识飞轮的文件相对路径）映射回本地文件与下载网关链接。"""
    if document_name:
        record = db.scalars(select(FileRecord).where(FileRecord.path == document_name, FileRecord.alive.is_(True)).order_by(FileRecord.id.desc())).first()
        if record is not None:
            return {"file_id": record.id, "link": f"/api/v1/gateway/files/{record.id}"}
    return {"file_id": None, "link": None}


def chat_with_references(db, query: str, top_k: int = 5) -> dict:
    """调用 Dify 问答；严格模式下无参考命中即拒绝回答。"""
    cfg = _config(db)
    if not cfg["base_url"] or not cfg["api_key"]:
        return {"configured": False, "error": "Dify 未配置", "answer": REFUSAL_ANSWER, "references": [], "hit": False, "blocked": True}

    payload = {"query": query, "response_mode": "blocking", "user": "knowledge-hub", "inputs": {}, "conversation_id": ""}
    try:
        r = httpx.post(urljoin(cfg["base_url"], "/v1/chat-messages"), json=payload, headers=_headers(cfg), timeout=30)
    except Exception as exc:  # noqa: BLE001
        return {"configured": True, "error": f"Dify 调用失败：{exc}", "answer": REFUSAL_ANSWER, "references": [], "hit": False, "blocked": True}
    if r.status_code >= 300:
        return {"configured": True, "error": f"Dify 返回 {r.status_code}: {r.text[:200]}", "answer": REFUSAL_ANSWER, "references": [], "hit": False, "blocked": True}

    data = r.json()
    resources = data.get("retriever_resources") or []
    references = []
    file_ids: set[int] = set()
    for res in resources:
        score = float(res.get("score") or 0)
        if cfg["min_score"] > 0 and score < cfg["min_score"]:
            continue
        ref = _resolve_reference(db, res.get("document_name") or "", res.get("content") or "")
        references.append({
            "document_id": res.get("document_id"),
            "document_name": res.get("document_name"),
            "segment_id": res.get("segment_id"),
            "content": (res.get("content") or "")[:500],
            "score": round(score, 6),
            "link": ref["link"],
            "file_id": ref["file_id"],
        })
        if ref["file_id"]:
            file_ids.add(ref["file_id"])

    hit = bool(references)
    chunk_ids: list[int] = []
    if file_ids:
        chunk_ids = list(db.scalars(select(DocumentChunk.id).where(DocumentChunk.file_id.in_(file_ids))).all())
    record_retrieval(db, query=query, hit=hit, chunk_ids=chunk_ids, source="dify")

    if cfg["strict"] and not hit:
        return {"configured": True, "answer": REFUSAL_ANSWER, "references": [], "hit": False, "blocked": True}
    return {"configured": True, "answer": data.get("answer"), "references": references, "hit": hit, "blocked": False}


def ingest_file_to_dify(db, file_id: int) -> dict:
    """把已解析文件的切片以文本形式接入 Dify 数据集（文档名 = 文件相对路径）。"""
    cfg = _config(db)
    if not cfg["base_url"] or not cfg["api_key"] or not cfg["dataset_id"]:
        raise ValueError("Dify 未配置完整（base_url / api_key / dataset_id）")
    record = db.get(FileRecord, file_id)
    if record is None:
        raise ValueError("file not found")
    chunks = db.scalars(
        select(DocumentChunk).where(DocumentChunk.file_id == file_id, DocumentChunk.file_hash == record.file_hash).order_by(DocumentChunk.chunk_index)
    ).all()
    if not chunks:
        raise ValueError("file has no parsed chunks yet")
    text = "\n\n".join(chunk.content for chunk in chunks)
    url = urljoin(cfg["base_url"], f"/v1/datasets/{cfg['dataset_id']}/document/create-by-text")
    payload = {
        "name": record.path,
        "text": text,
        "indexing_technique": "high_quality",
        "process_rule": {"mode": "automatic"},
        "doc_form": "text_model",
    }
    r = httpx.post(url, json=payload, headers=_headers(cfg), timeout=120)
    if r.status_code >= 300:
        raise ValueError(f"Dify 返回 {r.status_code}: {r.text[:200]}")
    return {"ok": True, "document_id": r.json().get("document", {}).get("id"), "file_id": file_id}
