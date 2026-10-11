"""Automated retention pruning for dated ``/archive/`` deltas.

Post-backup hook: enumerate every folder under ``remote:bucket/archive/`` and
``rclone purge`` any delta whose timestamp is older than ``RETAIN_DAYS``.
``/latest/`` is never touched.
"""

from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy import select

from app.config import settings
from app.database import SessionLocal
from app.models import Job
from app.rclone import archive_dates, purge_archive_date
from app.utils.path_utils import parse_archive_stamp


async def prune_archives(remote: str, bucket: str, retain_days: int) -> list[str]:
    """Delete archive deltas older than ``retain_days`` (kept on the cutoff day
    itself). Returns the names of the purged deltas."""
    cutoff = datetime.combine(
        date.today() - timedelta(days=retain_days), time.min, tzinfo=timezone.utc
    )
    purged: list[str] = []
    for d in await archive_dates(remote, bucket):
        stamp = parse_archive_stamp(d)
        if stamp is None:
            continue
        if stamp < cutoff:
            code = await purge_archive_date(remote, bucket, d)
            if code == 0:
                purged.append(d)  # only count deletions that actually succeeded
    return purged


async def run_retention() -> list[str]:
    """Retention hook: prune each enabled job's destination bucket (deduplicated),
    returning every purged archive delta name across buckets."""
    async with SessionLocal() as db:
        jobs = (
            await db.execute(select(Job).where(Job.enabled.is_(True)))
        ).scalars().all()
    seen: set[tuple[str, str]] = set()
    purged: list[str] = []
    for job in jobs:
        key = (job.dest_remote, job.dest_bucket)
        if key in seen:
            continue
        seen.add(key)
        try:
            purged.extend(
                await prune_archives(job.dest_remote, job.dest_bucket, settings.retain_days)
            )
        except Exception:  # noqa: BLE001
            continue
    return purged
