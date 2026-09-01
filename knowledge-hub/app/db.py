from collections.abc import Generator
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker, Session
from .config import settings


connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
engine = create_engine(settings.database_url, pool_pre_ping=True, connect_args=connect_args)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


class Base(DeclarativeBase):
    pass


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def initialize_database() -> None:
    """Create tables and apply the small, idempotent MVP migrations.

    ``create_all`` does not alter an existing PostgreSQL volume, so every
    process must run the same additive migrations before serving work.
    """
    backend = engine.url.get_backend_name()
    if backend == "postgresql":
        # pgvector is packaged in the PostgreSQL image but must be enabled per database.
        with engine.begin() as connection:
            connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    Base.metadata.create_all(bind=engine)
    if backend == "sqlite":
        columns_by_table = {
            "remote_commands": {"claimed_at": "DATETIME"},
            "tasks": {"result_json": "TEXT", "claimed_at": "DATETIME", "claimed_by": "VARCHAR(128)"},
            "files": {"alive": "BOOLEAN NOT NULL DEFAULT 1", "last_seen_at": "DATETIME", "category": "VARCHAR(128) NOT NULL DEFAULT '未分类'"},
            "document_chunks": {"file_hash": "VARCHAR(128) NOT NULL DEFAULT ''", "embedding": "TEXT", "embedding_provider": "VARCHAR(32)", "embedding_status": "VARCHAR(32) NOT NULL DEFAULT 'pending'", "embedded_at": "DATETIME", "hit_count": "INTEGER NOT NULL DEFAULT 0"},
        }
        inspector = inspect(engine)
        with engine.begin() as connection:
            for table, columns in columns_by_table.items():
                existing = {column["name"] for column in inspector.get_columns(table)}
                for name, sql_type in columns.items():
                    if name not in existing:
                        connection.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}"))
                if table == "files" and "last_seen_at" not in existing:
                    connection.execute(text("UPDATE files SET last_seen_at = CURRENT_TIMESTAMP WHERE last_seen_at IS NULL"))
        return

    if backend == "postgresql":
        statements = (
            "ALTER TABLE files ADD COLUMN IF NOT EXISTS alive BOOLEAN NOT NULL DEFAULT true",
            "ALTER TABLE files ADD COLUMN IF NOT EXISTS category VARCHAR(128) NOT NULL DEFAULT '未分类'",
            "ALTER TABLE files ADD COLUMN IF NOT EXISTS last_seen_at TIMESTAMPTZ",
            "UPDATE files SET last_seen_at = COALESCE(last_seen_at, updated_at, now()) WHERE last_seen_at IS NULL",
            "ALTER TABLE files ALTER COLUMN last_seen_at SET DEFAULT now()",
            "ALTER TABLE files ALTER COLUMN last_seen_at SET NOT NULL",
            "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS result_json JSONB",
            "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS claimed_at TIMESTAMPTZ",
            "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS claimed_by VARCHAR(128)",
            "ALTER TABLE remote_commands ADD COLUMN IF NOT EXISTS claimed_at TIMESTAMPTZ",
            "ALTER TABLE document_chunks ADD COLUMN IF NOT EXISTS file_hash VARCHAR(128) NOT NULL DEFAULT ''",
            "ALTER TABLE document_chunks ADD COLUMN IF NOT EXISTS embedding vector(1024)",
            "ALTER TABLE document_chunks ADD COLUMN IF NOT EXISTS embedding_provider VARCHAR(32)",
            "ALTER TABLE document_chunks ADD COLUMN IF NOT EXISTS embedding_status VARCHAR(32) NOT NULL DEFAULT 'pending'",
            "ALTER TABLE document_chunks ADD COLUMN IF NOT EXISTS embedded_at TIMESTAMPTZ",
            "ALTER TABLE document_chunks ADD COLUMN IF NOT EXISTS hit_count INTEGER NOT NULL DEFAULT 0",
            "UPDATE document_chunks SET file_hash = files.file_hash FROM files WHERE document_chunks.file_id = files.id AND document_chunks.file_hash = ''",
        )
        with engine.begin() as connection:
            for statement in statements:
                connection.execute(text(statement))
