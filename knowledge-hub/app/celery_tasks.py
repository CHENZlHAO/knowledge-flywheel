"""Celery entrypoint that executes persisted task IDs through the DB lease."""
from .config import settings

try:
    from celery import Celery
except ImportError:  # local minimal installs can continue in polling mode
    celery_app = None
else:
    celery_app = Celery("knowledge-hub", broker=settings.redis_url, backend=settings.redis_url)
    celery_app.conf.update(
        task_default_queue=settings.celery_queue,
        task_acks_late=True,
        task_reject_on_worker_lost=True,
        broker_connection_timeout=settings.celery_health_timeout_seconds,
        broker_connection_retry=False,
        task_publish_retry=False,
    )

    @celery_app.task(name=settings.celery_task_name, bind=True, autoretry_for=(), max_retries=0)
    def execute_persisted_task(self, task_id: int):
        from .db import SessionLocal, initialize_database
        from .services import run_task_by_id
        initialize_database()
        with SessionLocal() as db:
            task = run_task_by_id(db, int(task_id), f"celery:{self.request.id or 'worker'}", settings.task_lease_seconds)
            return {"task_id": task_id, "status": task.status if task else "already_claimed_or_finished"}
