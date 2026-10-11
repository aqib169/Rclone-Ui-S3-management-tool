import fcntl
import os
from pathlib import Path

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.config import settings

_instance_lock_fd: int | None = None


def acquire_instance_lock() -> None:
    """Refuse to start a second instance against the same data dir (spec §1)."""
    global _instance_lock_fd
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    fd = os.open(settings.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        raise RuntimeError(
            f"another orchestrator instance is already running on {settings.data_dir}"
        )
    _instance_lock_fd = fd


class Base(DeclarativeBase):
    pass


engine = create_async_engine(settings.sqlite_url, echo=False, pool_pre_ping=True)

SessionLocal = async_sessionmaker(
    engine, class_=AsyncSession, expire_on_commit=False
)


@event.listens_for(engine.sync_engine, "connect")
def _set_sqlite_pragmas(dbapi_connection, _connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute(f"PRAGMA busy_timeout={settings.busy_timeout_ms}")
    cursor.close()


async def _ensure_columns(conn) -> None:
    """Add columns introduced after a table already existed (create_all won't alter)."""
    additions = [
        ("remotes", "updated_at", "TIMESTAMP"),
        ("remotes", "options", "TEXT NOT NULL DEFAULT '{}'"),
        # Default bucket: a bucket-scoped Cloudflare R2 token cannot list
        # buckets, so the remote needs to be told which one to use.
        ("remotes", "bucket", "VARCHAR(128) NOT NULL DEFAULT ''"),
        ("runs", "archive_path", "VARCHAR(500) NOT NULL DEFAULT ''"),
        ("runs", "created_at", "TIMESTAMP"),
        # rclone operation surface: which subcommand ran + the flags chosen.
        ("runs", "operation", "VARCHAR(32) NOT NULL DEFAULT 'sync'"),
        ("runs", "flags", "TEXT NOT NULL DEFAULT '{}'"),
        ("audit_logs", "ip_address", "VARCHAR(64) NOT NULL DEFAULT ''"),
        ("audit_logs", "success", "BOOLEAN NOT NULL DEFAULT 1"),
    ]
    for table, column, decl in additions:
        info = (await conn.execute(text(f"PRAGMA table_info({table})"))).all()
        if column not in {row[1] for row in info}:
            await conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {decl}"))


async def init_db() -> None:
    acquire_instance_lock()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.logs_dir.mkdir(parents=True, exist_ok=True)
    async with engine.begin() as conn:
        await conn.execute(text("PRAGMA journal_mode=WAL"))
        await conn.execute(text("PRAGMA foreign_keys=ON"))
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with engine.begin() as conn:
        await _ensure_columns(conn)
        await _ensure_indexes(conn)
    from app.crypto import encrypt_plaintext_rows

    await encrypt_plaintext_rows()


async def _ensure_indexes(conn) -> None:
    indexes = [
        ("idx_audit_timestamp", "audit_logs", "timestamp"),
        ("idx_audit_action", "audit_logs", "action"),
        ("idx_runs_job_id", "runs", "job_id"),
        ("idx_runs_started_at", "runs", "started_at"),
    ]
    for name, table, column in indexes:
        await conn.execute(
            text(f"CREATE INDEX IF NOT EXISTS {name} ON {table} ({column})")
        )
