"""Deterministic center task worker.

The worker owns task leases and state transitions. LangGraph/Celery adapters can
be added behind ``execute_task_payload`` without changing the persistence
contract or the operator-facing task states.
"""
import time
from .config import settings
from .db import SessionLocal, initialize_database
from .services import reconcile_file_liveness, run_task_once, run_gap_summary
from .settings_store import get_effective_setting

_last_gap_summary = 0.0


def main() -> None:
    global _last_gap_summary
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
            # 定期汇总『常问但未命中参考』的问题，形成待添加清单
            interval_hours = float(get_effective_setting(db, "gap_summary_interval_hours", settings.gap_summary_interval_hours) or settings.gap_summary_interval_hours)
            interval_seconds = interval_hours * 3600.0
            if interval_seconds > 0 and time.time() - _last_gap_summary >= interval_seconds:
                try:
                    updated = run_gap_summary(db, window_hours=interval_hours)
                    if updated:
                        print(f"[gap-summary] aggregated {updated} gap item(s)")
                except Exception:
                    import logging

                    logging.getLogger("knowledge.worker").exception("gap summary cycle failed")
                _last_gap_summary = time.time()
        time.sleep(settings.worker_poll_seconds)


if __name__ == "__main__":
    main()
