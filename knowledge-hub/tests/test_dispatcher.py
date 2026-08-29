from app import celery_tasks, dispatcher
from app.config import settings
from app.db import SessionLocal
from app.models import AuditLog, Task


class FakeCelery:
    def __init__(self, error=None):
        self.error = error
        self.calls = []

    def send_task(self, name, args, queue):
        if self.error:
            raise self.error
        self.calls.append((name, args, queue))


def test_poll_mode_does_not_publish(monkeypatch):
    monkeypatch.setattr(settings, "task_execution_mode", "poll")
    fake = FakeCelery()
    monkeypatch.setattr(celery_tasks, "celery_app", fake)
    assert dispatcher.publish_task_id(123) is False
    assert fake.calls == []
    assert dispatcher.executor_health()["effective_mode"] == "poll"


def test_celery_mode_reports_poll_fallback_when_broker_is_down(monkeypatch):
    monkeypatch.setattr(settings, "task_execution_mode", "celery")
    monkeypatch.setattr(celery_tasks, "celery_app", FakeCelery())
    monkeypatch.setattr(dispatcher, "broker_reachable", lambda: False)
    health = dispatcher.executor_health()
    assert health["celery_adapter_loaded"] is True
    assert health["broker_reachable"] is False
    assert health["effective_mode"] == "poll_fallback"


def test_new_task_publishes_only_after_commit(monkeypatch):
    monkeypatch.setattr(settings, "task_execution_mode", "celery")
    fake = FakeCelery()
    monkeypatch.setattr(celery_tasks, "celery_app", fake)
    with SessionLocal() as db:
        task = Task(idempotency_key="post-commit-publish", kind="noop", payload_json="{}")
        db.add(task)
        db.flush()
        assert fake.calls == []
        task_id = task.id
        db.commit()
    assert fake.calls == [(settings.celery_task_name, [task_id], settings.celery_queue)]
    with SessionLocal() as db:
        audit = db.query(AuditLog).filter_by(resource_type="task", resource_id=str(task_id), action="task.published").one()
        assert audit.actor == "task-dispatcher"


def test_publish_failure_is_audited_and_task_stays_pending(monkeypatch):
    monkeypatch.setattr(settings, "task_execution_mode", "celery")
    monkeypatch.setattr(celery_tasks, "celery_app", FakeCelery(RuntimeError("redis unavailable")))
    with SessionLocal() as db:
        task = Task(idempotency_key="post-commit-fallback", kind="noop", payload_json="{}")
        db.add(task)
        db.commit()
        task_id = task.id
    with SessionLocal() as db:
        task = db.get(Task, task_id)
        assert task.status == "pending"
        audit = db.query(AuditLog).filter_by(resource_type="task", resource_id=str(task_id), action="task.publish_failed").one()
        assert "database_poll" in audit.detail


def test_rollback_never_publishes(monkeypatch):
    monkeypatch.setattr(settings, "task_execution_mode", "celery")
    fake = FakeCelery()
    monkeypatch.setattr(celery_tasks, "celery_app", fake)
    with SessionLocal() as db:
        db.add(Task(idempotency_key="rollback-no-publish", kind="noop", payload_json="{}"))
        db.flush()
        db.rollback()
    assert fake.calls == []


def test_requeued_task_is_published_after_commit(monkeypatch):
    monkeypatch.setattr(settings, "task_execution_mode", "celery")
    fake = FakeCelery()
    monkeypatch.setattr(celery_tasks, "celery_app", fake)
    with SessionLocal() as db:
        task = Task(idempotency_key="requeue-publish", kind="noop", payload_json="{}", status="failed")
        db.add(task)
        db.commit()
        fake.calls.clear()
        task.status = "pending"
        task.error = None
        db.commit()
        assert fake.calls == [(settings.celery_task_name, [task.id], settings.celery_queue)]
