"""Optional post-commit Celery delivery for persisted tasks.

The database task row is written first and remains the source of truth. This
module only accelerates delivery; the polling worker is always a valid recovery
path when the broker or Celery package is unavailable.
"""
import json
import logging
from sqlalchemy import event, inspect as sa_inspect
from sqlalchemy.orm import Session as OrmSession

from .config import settings
from .db import SessionLocal
from .models import AuditLog, Task

logger = logging.getLogger(__name__)


def celery_available() -> bool:
    if not settings.celery_enabled:
        return False
    try:
        from .celery_tasks import celery_app
    except Exception:
        return False
    return celery_app is not None


def broker_reachable() -> bool:
    if not settings.celery_enabled:
        return False
    try:
        from redis import Redis
        client = Redis.from_url(
            settings.redis_url,
            socket_connect_timeout=settings.celery_health_timeout_seconds,
            socket_timeout=settings.celery_health_timeout_seconds,
        )
        return bool(client.ping())
    except Exception:
        return False


def executor_health() -> dict:
    mode = settings.task_execution_mode.strip().lower()
    adapter_loaded = celery_available()
    reachable = broker_reachable() if adapter_loaded else False
    return {
        "configured_mode": mode,
        "celery_adapter_loaded": adapter_loaded,
        "broker_reachable": reachable,
        "effective_mode": "celery_with_poll_fallback" if mode in {"celery", "auto"} and reachable else "poll_fallback" if mode in {"celery", "auto"} else "poll",
        "database_poll_fallback": True,
        "queue": settings.celery_queue,
    }


def _dispatch_audit(task_id: int, action: str, detail: dict) -> None:
    try:
        with SessionLocal() as db:
            db.add(AuditLog(actor="task-dispatcher", action=action, resource_type="task", resource_id=str(task_id), detail=json.dumps(detail, ensure_ascii=False)))
            db.commit()
    except Exception:
        logger.exception("could not persist task dispatch audit for task %s", task_id)


def publish_task_id(task_id: int) -> bool:
    """Publish a task ID after commit; return whether Celery accepted it."""
    if not settings.celery_enabled:
        return False
    try:
        from .celery_tasks import celery_app
        if celery_app is None:
            raise RuntimeError("celery package is unavailable")
        celery_app.send_task(settings.celery_task_name, args=[task_id], queue=settings.celery_queue)
    except Exception as exc:
        logger.warning("Celery publish failed for task %s; database polling remains active: %s", task_id, exc)
        _dispatch_audit(task_id, "task.publish_failed", {"error": str(exc), "fallback": "database_poll"})
        return False
    _dispatch_audit(task_id, "task.published", {"queue": settings.celery_queue})
    return True


@event.listens_for(OrmSession, "before_flush")
def _collect_new_tasks(session, _flush_context, _instances):
    if not settings.celery_enabled:
        return
    pending = session.info.setdefault("_new_task_objects", {})
    for obj in session.new:
        if isinstance(obj, Task):
            pending[id(obj)] = obj
    for obj in session.dirty:
        if not isinstance(obj, Task):
            continue
        history = sa_inspect(obj).attrs.status.history
        if history.has_changes() and obj.status == "pending":
            pending[id(obj)] = obj


@event.listens_for(OrmSession, "after_flush_postexec")
def _materialize_task_ids(session, _flush_context):
    if not settings.celery_enabled:
        return
    objects = session.info.get("_new_task_objects", {})
    ids = session.info.setdefault("_task_ids_to_publish", set())
    for obj in objects.values():
        if obj.id is not None:
            ids.add(obj.id)


@event.listens_for(OrmSession, "after_commit")
def _publish_after_commit(session):
    ids = session.info.pop("_task_ids_to_publish", set())
    session.info.pop("_new_task_objects", None)
    for task_id in ids:
        publish_task_id(task_id)


@event.listens_for(OrmSession, "after_rollback")
def _discard_uncommitted_tasks(session):
    session.info.pop("_task_ids_to_publish", None)
    session.info.pop("_new_task_objects", None)
