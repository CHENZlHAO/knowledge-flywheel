"""Standalone single-process center for LAN first-generation deployments.

Double-click the packaged ``knowledge-center`` executable (Windows) and the
center starts serving the API + operator console while a background thread runs
the deterministic worker. No Docker, PostgreSQL, Redis, or Ollama is required:
it defaults to SQLite storage and the explicitly-labelled deterministic
embedding fallback.

Production (PostgreSQL/pgvector + Ollama + Celery + MQTT) still uses the
Docker Compose deployment; this file is the "开箱即用" LAN entrypoint.
"""
from __future__ import annotations

import logging
import os
import threading
import time
import webbrowser

# Force offline-safe first-generation defaults BEFORE importing app.config, so a
# stray production .env cannot point the standalone build at unreachable services.
os.environ.setdefault("APP_ENV", "development")
os.environ.setdefault("DATABASE_URL", "sqlite:///./knowledge-hub.db")
os.environ.setdefault("STORAGE_ROOT", "./storage")
os.environ.setdefault("BACKUP_DIR", "./backups")
os.environ.setdefault("EMBEDDING_PROVIDER", "deterministic")
os.environ.setdefault("ORCHESTRATION_MODE", "state_machine")
os.environ.setdefault("TASK_EXECUTION_MODE", "poll")


def _run_worker() -> None:
    from app.config import settings
    from app.db import SessionLocal, initialize_database

    initialize_database()
    while True:
        try:
            from app.alerting import process_alert_deliveries
            from app.services import reconcile_file_liveness, run_task_once

            with SessionLocal() as db:
                run_task_once(db, settings.worker_id, settings.task_lease_seconds)
                reconcile_file_liveness(
                    db,
                    settings.file_missing_after_seconds,
                    settings.fixed_replica_node_ids,
                )
                process_alert_deliveries(db)
        except Exception:  # the worker loop must never die; the API keeps serving
            logging.getLogger("standalone-center").exception("worker cycle failed")
        time.sleep(settings.worker_poll_seconds)


def _open_console(url: str) -> None:
    time.sleep(1.5)
    try:
        webbrowser.open(url)
    except Exception:
        pass


def main() -> None:
    import uvicorn

    from app.config import settings
    from app.db import initialize_database
    from app.main import app

    initialize_database()
    threading.Thread(target=_run_worker, daemon=True, name="center-worker").start()
    url = f"http://127.0.0.1:{settings.api_port}/"
    threading.Thread(target=_open_console, args=(url,), daemon=True).start()
    print(f"Knowledge Hub 中心已启动：{url}")
    print(f"局域网其他设备访问：http://<本机IP>:{settings.api_port}/")
    uvicorn.run(app, host=settings.api_host, port=settings.api_port, log_level="info")


if __name__ == "__main__":
    main()
