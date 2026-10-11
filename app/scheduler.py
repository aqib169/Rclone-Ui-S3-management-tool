"""Scheduler + run orchestration.

Responsible for:
  * APScheduler lifecycle (cron jobs, retention hook, ops, self-backup).
  * Safe cron parsing/registration (with auto-sanitization of ``* */X``).
  * Run creation and dispatch to the service engines
    (``app.services.backup`` / ``app.services.restore``).

The actual rclone transfers, restore reconstruction and retention pruning live
in the service modules; this module only coordinates them.
"""

import asyncio
import json
import os
import shutil
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select

from app.audit import log_background
from app.config import settings
from app.database import SessionLocal
from app.models import AuditLog, Job, Run, target_str, utcnow
from app.schemas import sanitize_cron_schedule
from app.services.backup import execute_backup
from app.services.common import cancel_run  # noqa: F401  (re-exported for routers)
from app.services.locks import TargetBusy, claim, mutating_targets, release
from app.services.restore import execute_restore
from app.services.retention import run_retention
from app.services.transfer import execute_operation
from app.utils.path_utils import get_latest_path

scheduler = AsyncIOScheduler(timezone="UTC")

# Strong references to fire-and-forget run tasks. asyncio only keeps weak
# refs to running tasks, so without this the event loop may garbage-collect
# a mid-flight backup/restore coroutine (Python docs: "Save a reference to
# the result of this function, to avoid a task disappearing mid-execution").
_background_tasks: set[asyncio.Task] = set()


# ---------------------------------------------------------------- targets
def latest_destination(dest_remote: str, dest_bucket: str, dest_path: str) -> str:
    """The static current-state mirror that is the backup sync target:
    remote:bucket/latest[/dest_path]."""
    return get_latest_path(target_str(dest_remote, dest_bucket, dest_path))


# ---------------------------------------------------------------- cron
def _translate_dow(field: str) -> str:
    """crontab uses 0=Sunday; APScheduler uses 0=Monday. Map a cron dow field."""
    parts = []
    for part in field.split(","):
        part = part.strip()
        step = ""
        base = part
        if "/" in part:
            base, _, s = part.partition("/")
            step = "/" + s
        if base == "*":
            parts.append("*" + step)
        elif "-" in base:
            a, _, b = base.partition("-")
            parts.append(f"{(int(a) - 1) % 7}-{(int(b) - 1) % 7}{step}")
        else:
            parts.append(f"{(int(base) - 1) % 7}{step}")
    return ",".join(parts)


def build_cron_trigger(cron: str) -> CronTrigger | None:
    cron = sanitize_cron_schedule(cron or "")
    parts = cron.split()
    if len(parts) != 5:
        return None
    try:
        return CronTrigger(
            minute=parts[0],
            hour=parts[1],
            day=parts[2],
            month=parts[3],
            day_of_week=_translate_dow(parts[4]),
        )
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------- run lifecycle
async def create_run(
    source: str,
    destination: str,
    kind: str = "backup",
    job_id: int | None = None,
    triggered_by: str = "manual",
    operation: str = "sync",
    flags: dict | None = None,
) -> str:
    """Persist a run row and dispatch it to the matching service engine.

    Raises `TargetBusy` when another run is already modifying the same path, so
    two syncs can never delete each other's files.
    """
    run_id = str(uuid.uuid4())
    claim(run_id, mutating_targets(operation, source, destination))
    try:
        async with SessionLocal() as db:
            run = Run(
                run_id=run_id,
                job_id=job_id,
                kind=kind,
                operation=operation,
                flags=json.dumps(flags or {}),
                source=source,
                destination=destination,
                status="pending",
                triggered_by=triggered_by,
            )
            db.add(run)
            await db.commit()
    except Exception:
        release(run_id)
        raise
    if kind == "backup":
        executor = execute_backup
    elif kind == "restore":
        executor = execute_restore
    else:
        executor = execute_operation
    task = asyncio.create_task(executor(run_id), name=f"run-{run_id}")
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return run_id


# ---------------------------------------------------------------- scheduled jobs
async def _scheduled_job(job_id: int) -> None:
    async with SessionLocal() as db:
        job = await db.get(Job, job_id)
        if job is None or not job.enabled:
            return
        source = target_str(job.source_remote, job.source_bucket, job.source_path)
        destination = latest_destination(job.dest_remote, job.dest_bucket, job.dest_path)
        name = job.name
    try:
        run_id = await create_run(
            source, destination, kind="backup", job_id=job_id, triggered_by="schedule"
        )
    except TargetBusy as busy:
        # Another run is already writing to this destination; skip instead of
        # letting two syncs delete each other's files.
        await log_background(
            action="run_skipped",
            details=f"job={name} reason=busy target={busy.target}",
            status_code=409,
        )
        return
    await log_background(action="run_job", details=f"job={name} run_id={run_id}")


def schedule_job(job_id: int, cron: str) -> bool:
    if not cron:
        return False
    trigger = build_cron_trigger(cron)
    if trigger is None:
        return False
    scheduler.add_job(
        _scheduled_job,
        trigger,
        args=[job_id],
        id=f"job-{job_id}",
        replace_existing=True,
    )
    return True


def unschedule_job(job_id: int) -> None:
    try:
        scheduler.remove_job(f"job-{job_id}")
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------- startup / teardown
async def reconcile_runs_on_startup() -> int:
    """Mark orphaned runs (pending/running from a previous process) as failed.

    Runs must be reconciled BEFORE the scheduler starts so that a run left
    'running' by a crash is never mistaken for an active one.
    """
    async with SessionLocal() as db:
        rows = (
            await db.execute(select(Run).where(Run.status.in_(["pending", "running"])))
        ).scalars().all()
        for run in rows:
            run.status = "failed"
            run.finished_at = utcnow()
            run.exit_code = -1
            run.error = "Orchestrator restarted during execution"
        await db.commit()
        return len(rows)


def reschedule_self_backup() -> bool:
    """Register/unregister the daily self-backup job based on the current
    `self_backup_dest` setting. Returns True if the job is now scheduled."""
    job_id = "self-backup"
    scheduled = bool((settings.self_backup_dest or "").strip())
    if scheduled and scheduler.get_job(job_id) is None:
        scheduler.add_job(
            _self_backup_job,
            CronTrigger(hour=3, minute=10),
            id=job_id,
            replace_existing=True,
        )
    elif not scheduled and scheduler.get_job(job_id) is not None:
        scheduler.remove_job(job_id)
    return scheduled


async def start_scheduler() -> None:
    scheduler.start()
    scheduler.add_job(
        _retention_job, CronTrigger(hour=3, minute=0), id="retention", replace_existing=True
    )
    scheduler.add_job(
        _ops_job, CronTrigger(hour=3, minute=5), id="ops", replace_existing=True
    )
    reschedule_self_backup()
    async with SessionLocal() as db:
        jobs = (
            await db.execute(
                select(Job).where(Job.enabled.is_(True), Job.schedule != "")
            )
        ).scalars().all()
        for job in jobs:
            schedule_job(job.id, job.schedule)


async def _ops_job() -> None:
    await prune_old_logs()


async def prune_old_logs(retain_days: int | None = None) -> int:
    """Delete run log files older than the retention window."""
    retain = retain_days or settings.log_retain_days
    cutoff = utcnow().timestamp() - retain * 86400
    removed = 0
    if not settings.logs_dir.exists():
        return 0
    for path in settings.logs_dir.glob("run_*.log"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError:
            continue
    return removed


async def _self_backup_job() -> None:
    """Daily snapshot of the orchestrator's own data dir to a remote.

    The SQLite DB is snapshotted via the online backup API so rclone never
    reads a live, mutating file (which aborts a sync with "source file is
    being updated"). The snapshot dir is rebuilt on each run.
    """
    dest = (settings.self_backup_dest or "").strip()
    if not dest:
        return
    snapshot = settings.data_dir / "self_backup_snapshot"
    try:
        shutil.rmtree(snapshot, ignore_errors=True)
        snapshot.mkdir(parents=True, exist_ok=True)
        src = sqlite3.connect(f"file:{settings.db_path}?mode=ro", uri=True)
        dst = sqlite3.connect(snapshot / "orchestrator.db")
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
        for item in settings.data_dir.iterdir():
            if item.name == snapshot.name:
                continue
            if item.is_file():
                shutil.copy2(item, snapshot / item.name)
    except Exception:  # noqa: BLE001
        return
    run_id = await create_run(
        str(snapshot),
        dest,
        kind="backup",
        triggered_by="self-backup",
    )
    await log_background(action="self_backup", details=f"dest={dest} run_id={run_id}")


async def _retention_job() -> None:
    """Prune expired archive deltas for every enabled job's destination bucket."""
    await run_retention()


def stop_scheduler() -> None:
    scheduler.shutdown(wait=False)
