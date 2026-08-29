import os

os.environ.setdefault("DATABASE_URL", "sqlite:///./test.db")
# Tests assume development-mode open access; a production .env must not leak in.
os.environ.setdefault("APP_ENV", "development")

import pytest

from app.db import Base, SessionLocal, engine
from app.models import Alert, AlertDelivery, ApiKey, AuditLog, BlobObject, DocumentChunk, FileRecord, FileReplica, Node, Proposal, RemoteCommand, Task, FlywheelEvent


@pytest.fixture(autouse=True)
def clean_database():
    """Keep API tests deterministic when using the repository-local SQLite DB."""
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        for model in (Alert, AlertDelivery, ApiKey, AuditLog, BlobObject, DocumentChunk, FileReplica, RemoteCommand, Task, Proposal, FlywheelEvent, FileRecord, Node):
            db.query(model).delete(synchronize_session=False)
        db.commit()
    yield
