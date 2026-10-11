"""Differential mirror backup engine.

Backs a source remote/path into the destination's ``/latest/`` mirror using
``rclone sync``. Every file that is overwritten or deleted during the sync is
archived under ``/archive/<timestamp>/`` via rclone's ``--backup-dir``, giving
a point-in-time delta without duplicating the full tree.

This module only decides *what* to run: it resolves the run's source,
destination and archive delta, persists them on the run row and hands over to
the shared runner in ``app.services.transfer`` (which spawns rclone, streams
the JSON log into live progress and records the outcome).
"""

from sqlalchemy import select

from app.database import SessionLocal
from app.models import Job, Run, target_str, utcnow
from app.services.common import mark_run
from app.services.locks import release
from app.services.transfer import execute_operation
from app.utils.path_utils import (
    get_archive_path,
    get_base_target,
    get_latest_path,
    is_latest_target,
    timestamp_now,
)


def differential_backup_dir(destination: str) -> str | None:
    """The ``--backup-dir`` archive delta path for a ``/latest/`` sync target,
    or None when the destination is not a ``/latest/`` mirror (plain sync)."""
    if not is_latest_target(destination):
        return None
    return get_archive_path(get_base_target(destination), timestamp_now())


async def execute_backup(run_id: str) -> None:
    """Resolve a backup run's targets and execute it through the shared runner."""
    async with SessionLocal() as db:
        run = (
            await db.execute(select(Run).where(Run.run_id == run_id))
        ).scalar_one_or_none()
        if run is None:
            return
        job = await db.get(Job, run.job_id) if run.job_id else None
        if run.source and run.destination:
            source, destination = run.source, run.destination
        elif job:
            source = target_str(job.source_remote, job.source_bucket, job.source_path)
            # Scheduled/manual job runs always sync into the /latest/ mirror.
            destination = get_latest_path(
                target_str(job.dest_remote, job.dest_bucket, job.dest_path)
            )
        else:
            # No usable source/destination: fail the run instead of leaving it
            # pending forever.
            release(run_id)
            await mark_run(
                run_id,
                status="failed",
                finished_at=utcnow(),
                exit_code=-1,
                error="backup has no source/destination and no matching job",
            )
            return

        archive_path = ""
        if run.job_id is not None:
            archive_path = differential_backup_dir(destination) or ""

        run.source = source
        run.destination = destination
        run.archive_path = archive_path
        run.operation = "sync"
        await db.commit()

    await execute_operation(run_id)
