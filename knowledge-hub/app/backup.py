"""Backup and restore support.

A backup run produces a timestamped directory containing:

* ``database.dump`` — SQLite file copy or PostgreSQL ``pg_dump`` (compressed).
* ``storage.tar.gz`` — the configured object-storage root.
* ``manifest.json`` — file list with sizes and SHA-256 checksums plus metadata.

Retention is pruned by ``BACKUP_RETENTION_DAYS``. The matching restore procedure
lives in ``scripts/restore.sh``; see ``docs/operations.md`` for RPO/RTO targets.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import shutil
import subprocess
import tarfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .config import settings
from .db import engine


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _database_dump(target: Path) -> Path:
    backend = engine.url.get_backend_name()
    if backend == "sqlite":
        database = engine.url.database
        source = Path(database)
        if not source.is_absolute():
            source = Path.cwd() / source
        if not source.exists():
            raise FileNotFoundError(f"SQLite database not found: {source}")
        if target.suffix == ".gz":
            with source.open("rb") as src, gzip.open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)
        else:
            shutil.copyfile(source, target)
        return target
    if backend == "postgresql":
        url = engine.url
        env = {
            "PGPASSWORD": url.password or "",
            "PGHOST": url.host or "localhost",
            "PGPORT": str(url.port or 5432),
            "PGUSER": url.username or "",
            "PGDATABASE": url.database or "",
        }
        with target.open("wb") as handle:
            subprocess.run(
                ["pg_dump", "--no-owner", "--no-privileges", "--format=custom", "--file=-"],
                stdout=handle,
                env={**os.environ, **env},
                check=True,
            )
        return target
    raise RuntimeError(f"backup is not supported for database backend: {backend}")


def _prune(root: Path, retention_days: int) -> int:
    if retention_days <= 0:
        return 0
    cutoff = time.time() - retention_days * 86400
    removed = 0
    for child in root.iterdir():
        if not child.is_dir():
            continue
        try:
            if child.stat().st_mtime < cutoff:
                shutil.rmtree(child)
                removed += 1
        except OSError:
            continue
    return removed


def perform_backup() -> dict[str, Any]:
    root = Path(settings.backup_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = root / stamp
    run_dir.mkdir(exist_ok=False)

    db_file = _database_dump(run_dir / "database.dump")
    storage_tar = run_dir / "storage.tar.gz"
    storage_root = Path(settings.storage_root).resolve()
    with tarfile.open(storage_tar, "w:gz") as tar:
        if storage_root.exists():
            tar.add(storage_root, arcname="storage")

    files = [db_file, storage_tar]
    manifest = {
        "backup_id": stamp,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "database_backend": engine.url.get_backend_name(),
        "files": [
            {"name": path.name, "size_bytes": path.stat().st_size, "sha256": _sha256_file(path)}
            for path in files
        ],
        "storage_root": str(storage_root),
        "retention_days": settings.backup_retention_days,
    }
    (run_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    pruned = _prune(root, settings.backup_retention_days)
    manifest["pruned_runs"] = pruned
    return manifest


def backup_status() -> dict[str, Any]:
    root = Path(settings.backup_dir).resolve()
    runs = []
    if root.exists():
        for manifest_path in sorted(root.glob("*/manifest.json"), reverse=True):
            try:
                runs.append(json.loads(manifest_path.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                continue
    return {"backup_dir": str(root), "retention_days": settings.backup_retention_days, "runs": runs}
