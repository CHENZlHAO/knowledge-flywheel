"""Deterministic center task worker.

The worker owns task leases and state transitions. LangGraph/Celery adapters can
be added behind ``execute_task_payload`` without changing the persistence
contract or the operator-facing task states.
"""
import time
from .config import settings
from .db import SessionLocal, initialize_database
from .services import reconcile_file_liveness, run_task_once


def main() -> None:
    initialize_database()
    while True:
        with SessionLocal() as db:
            # Celery accelerates delivery, while this poller remains the
            # recovery path for broker outages and lost messages.
            run_task_once(db, settings.worker_id, settings.task_lease_seconds)
            reconcile_file_liveness(db, settings.file_missing_after_seconds, settings.fixed_replica_node_ids)
            try:
                from .alerting import process_alert_deliveries

                process_alert_deliveries(db)
            except Exception:  # alert delivery must never stop the task loop
                import logging

                logging.getLogger("knowledge.worker").exception("alert delivery cycle failed")
        time.sleep(settings.worker_poll_seconds)


if __name__ == "__main__":
    main()
