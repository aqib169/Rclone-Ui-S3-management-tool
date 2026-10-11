"""Unified rclone runner: one code path for every operation.

Whatever the user started — a scheduled backup (`sync`), a restore (`copy`), an
ad-hoc `move`/`delete`/`check` from the Transfer Console — it ends up here:

    Run row (operation + source + destination + flags)
      -> validated argv            (app.operations)
      -> rclone process            (app.rclone.spawn_argv)
      -> JSON log streamed         (app.rclone.follow_log)
      -> live progress             (app.progress -> app.state.run_progress)
      -> run marked success/failed with files + bytes

Keeping one runner is what lets the UI show the same progress bar, cancellation
and history for every operation instead of special-casing backups.
"""

import asyncio
import json
import os

from sqlalchemy import select

from app.config import settings
from app.database import SessionLocal
from app.models import Job, Run, utcnow
from app.operations import build_argv, get_operation, validate_flags
from app.progress import empty_progress, parse_line, progress_from_entry, read_progress
from app.rclone import drain, follow_log, spawn_argv, write_temp_config
from app.services.common import append_log, mark_run
from app.services.locks import release
from app.state import active_runs, clear_progress, set_progress


def run_flags(run: Run) -> dict:
    """The validated flag map stored on a run row."""
    try:
        stored = json.loads(run.flags or "{}")
    except (json.JSONDecodeError, TypeError):
        stored = {}
    if not isinstance(stored, dict):
        stored = {}
    try:
        return validate_flags(stored, run.operation or "sync")
    except ValueError:
        # A flag that is no longer supported (e.g. after an app downgrade)
        # must not block the run: fall back to no extra flags.
        return {}


def _unlink(path) -> None:
    if not path:
        return
    try:
        os.unlink(path)
    except OSError:
        pass


async def execute_operation(run_id: str) -> None:
    """Execute the operation recorded on a run, with live progress."""
    async with SessionLocal() as db:
        run = (
            await db.execute(select(Run).where(Run.run_id == run_id))
        ).scalar_one_or_none()
        if run is None:
            return
        operation = run.operation or "sync"
        source, destination = run.source, run.destination
        archive_path = run.archive_path
        job_name = "(ad-hoc)"
        if run.job_id:
            job = await db.get(Job, run.job_id)
            if job is not None:
                job_name = job.name
        log_path = settings.logs_dir / f"run_{run_id}.log"
        run.status = "running"
        run.started_at = utcnow()
        run.log_path = str(log_path)
        await db.commit()

    spec = get_operation(operation)
    extra: list[str] = ["--backup-dir", archive_path] if archive_path else []

    try:
        flags = run_flags(run)
        argv = build_argv(operation, source, destination, flags, extra)
    except Exception as exc:  # noqa: BLE001 - never leave a run pending
        release(run_id)
        await mark_run(
            run_id,
            status="failed",
            finished_at=utcnow(),
            exit_code=-1,
            error=f"invalid {operation} request: {exc}",
        )
        return

    append_log(
        log_path,
        f"=== {operation}: {source} -> {destination or '(none)'} ===",
    )

    config_path = None
    try:
        config_path = await write_temp_config()
        proc = await spawn_argv(
            argv,
            log_path,
            config_path,
            defaults={"transfers": flags.get("transfers")},
        )
    except Exception as exc:  # noqa: BLE001
        _unlink(config_path)
        release(run_id)
        await mark_run(
            run_id,
            status="failed",
            finished_at=utcnow(),
            exit_code=-1,
            error=str(exc),
        )
        return

    active_runs[run_id] = {
        "proc": proc,
        "kind": spec.category if spec else "Transfer",
        "operation": operation,
        "source": source,
        "destination": destination,
        "job_name": job_name,
    }

    # Stream the JSON log into live progress while rclone works.
    async def _on_line(raw: str) -> None:
        entry = parse_line(raw)
        if entry is None:
            return
        progress = progress_from_entry(entry)
        if progress:
            set_progress(run_id, progress)

    follower = asyncio.create_task(follow_log(log_path, _on_line))
    code = -1
    try:
        drainer = asyncio.create_task(drain(proc))
        code = await proc.wait()
        await drainer
    finally:
        follower.cancel()
    _unlink(config_path)

    entry = active_runs.pop(run_id, None)
    progress = read_progress(log_path)
    release(run_id)  # the path is free again

    if entry and entry.get("cancelled"):
        await mark_run(
            run_id,
            status="cancelled",
            finished_at=utcnow(),
            exit_code=code,
            files=progress["files"],
            size_bytes=progress["bytes"],
        )
        clear_progress(run_id)
        return

    await mark_run(
        run_id,
        status="success" if code == 0 else "failed",
        finished_at=utcnow(),
        exit_code=code,
        error="" if code == 0 else error_summary(log_path),
        files=progress["files"],
        size_bytes=progress["bytes"],
    )
    if code == 0:
        # The audit trail had a trigger and a failure entry but no completion.
        await log_result(run_id, "completed", progress)


async def log_result(run_id: str, result: str, progress: dict) -> None:
    """Audit a finished restore (the trail only had trigger/failure before)."""
    from app.database import SessionLocal
    from app.models import AuditLog

    async with SessionLocal() as db:
        db.add(
            AuditLog(
                username="system",
                method="SCHEDULER",
                action=f"restore_{result}",
                details=(
                    f"run_id={run_id} files={progress.get('files', 0)} "
                    f"bytes={progress.get('bytes', 0)}"
                ),
            )
        )
        await db.commit()


def error_summary(log_path, limit: int = 12) -> str:
    """Last error-ish lines of a run log, for the `runs.error` column."""
    from app.progress import message_from_entry
    from app.rclone import tail

    lines: list[str] = []
    for raw in tail(log_path, lines=400).splitlines():
        entry = parse_line(raw)
        if entry is None:
            if "ERROR" in raw or "Failed" in raw:
                lines.append(raw.strip())
        elif str(entry.get("level")) == "error":
            message = message_from_entry(entry)
            name = entry.get("object")
            lines.append(f"{message}: {name}" if name else message)
        if len(lines) >= limit:
            break
    return " | ".join(lines[-limit:]) or "operation failed"


def progress_for(run_id: str, log_path: str = "") -> dict:
    """Live progress from memory, falling back to the run's log file.

    The log fallback means progress still works after a restart or for a run
    started by the scheduler (which has no in-memory snapshot yet).
    """
    from app.state import run_progress

    snapshot = run_progress.get(run_id)
    if snapshot:
        return snapshot
    return read_progress(log_path) if log_path else empty_progress()