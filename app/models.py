from datetime import datetime, timezone

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def target_str(remote: str, bucket: str = "", path: str = "") -> str:
    bucket = (bucket or "").strip("/")
    path = (path or "").strip("/")
    parts = [p for p in (bucket, path) if p]
    return f"{remote}:{'/'.join(parts)}"


class Remote(Base):
    __tablename__ = "remotes"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    # Any rclone backend name ("s3", "local", "b2", "google cloud storage", …).
    # Widened from 16 because some backend names are longer.
    type: Mapped[str] = mapped_column(String(64), default="s3")
    provider: Mapped[str] = mapped_column(String(32), default="Other")
    endpoint: Mapped[str] = mapped_column(String(256))
    access_key: Mapped[str] = mapped_column(String(128))
    secret_key: Mapped[str] = mapped_column(String(128))
    region: Mapped[str] = mapped_column(String(64), default="garage")
    # Default bucket for bucket-based backends. A bucket-scoped Cloudflare R2
    # token cannot list buckets, so without this the remote has no usable root.
    bucket: Mapped[str] = mapped_column(String(128), default="")
    # Full rclone config option map as JSON. Holds every non-S3 backend's
    # settings and acts as an overlay for the legacy columns above, so existing
    # S3/local remotes keep working untouched. Values of secret options are
    # Fernet-encrypted exactly like access_key/secret_key.
    options: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow
    )


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    source_remote: Mapped[str] = mapped_column(String(64))
    source_bucket: Mapped[str] = mapped_column(String(500), default="")
    source_path: Mapped[str] = mapped_column(String(500), default="")
    dest_remote: Mapped[str] = mapped_column(String(64))
    dest_bucket: Mapped[str] = mapped_column(String(500), default="")
    dest_path: Mapped[str] = mapped_column(String(500), default="")
    schedule: Mapped[str] = mapped_column(String(100), default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow
    )

    runs: Mapped[list["Run"]] = relationship(
        back_populates="job", cascade="all, delete-orphan", passive_deletes=True
    )


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[str] = mapped_column(String(36), unique=True, index=True)
    job_id: Mapped[int | None] = mapped_column(
        ForeignKey("jobs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    kind: Mapped[str] = mapped_column(String(20), default="backup")
    # rclone subcommand that produced this run ("sync", "copy", "move",
    # "delete", "check", …). Backups are always "sync".
    operation: Mapped[str] = mapped_column(String(32), default="sync")
    # JSON dict of the extra rclone flags selected for this run.
    flags: Mapped[str] = mapped_column(Text, default="{}")
    source: Mapped[str] = mapped_column(String(500), default="")
    destination: Mapped[str] = mapped_column(String(500), default="")
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    triggered_by: Mapped[str] = mapped_column(String(20), default="manual")
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    files: Mapped[int] = mapped_column(Integer, default=0)
    size_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    archive_path: Mapped[str] = mapped_column(String(500), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    log_path: Mapped[str] = mapped_column(String(500), default="")
    error: Mapped[str] = mapped_column(Text, default="")

    job: Mapped["Job | None"] = relationship(back_populates="runs")


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    username: Mapped[str] = mapped_column(String(100), default="")
    ip_address: Mapped[str] = mapped_column(String(64), default="")
    method: Mapped[str] = mapped_column(String(10), default="")
    path: Mapped[str] = mapped_column(String(500), default="")
    action: Mapped[str] = mapped_column(String(100), default="")
    details: Mapped[str] = mapped_column(Text, default="")
    status_code: Mapped[int] = mapped_column(Integer, default=0)
    success: Mapped[bool] = mapped_column(Boolean, default=True)


class Setting(Base):
    """UI-managed overrides for the app's runtime settings (env vars are the
    bootstrap default; a row here wins)."""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow
    )