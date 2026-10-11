"""Shared helpers for backup/restore run execution: run lifecycle bookkeeping
in SQLite (status, timestamps, transfer summary, archive path), log appends,
and cancellation against the in-memory active-run registry."""

import os
import re
from pathlib import Path

from sqlalchemy import select

from app.database import SessionLocal
from app.models import AuditLog, Run, utcnow
from app.state import active_runs

_BYTES_RE = re.compile(r"([\d.]+\s+\S+)\s*/\s*([\d.]+\s+\S+),\s*(\d+)%")
_FILES_RE = re.compile(r"(\d+)\s*/\s*(\d+),\s*(\d+)%")
_UNITS = {"Bytes": 1, "B": 1, "KiB": 1024, "MiB": 1024**2, "GiB": 1024**3, "TiB": 1024**4}


def parse_transfer_summary(log_text: str) -> tuple[int, int]:
    """Extract (files, bytes) from the final rclone stats lines."""
    size = 0
    for line in log_text.splitlines():
        m = _BYTES_RE.search(line)
        if not m:
            continue
        try:
            amount, unit = m.group(1).split()
            size = int(float(amount) * _UNITS.get(unit, 1))
        except (ValueError, IndexError):
            size = 0
    files = 0
    matches = list(_FILES_RE.finditer(log_text))
    if matches:
        files = int(matches[-1].group(2))
    else:
        # fast transfers end before a periodic stats line is printed; fall back
        # to counting the per-file "Copied" log lines.
        files = sum(1 for line in log_text.splitlines() if ": Copied (" in line)
    return files, size


def append_log(log_path: Path, text: str) -> None:
    try:
        with log_path.open("a", errors="replace") as f:
            f.write(text + "\n")
    except OSError:
        pass


async def mark_run(run_id: str, **fields) -> None:
    async with SessionLocal() as db:
        run = (await db.execute(select(Run).where(Run.run_id == run_id))).scalar_one_or_none()
        if run is None:
            return
        for key, value in fields.items():
            setattr(run, key, value)
        await db.commit()


async def mark_restore_failed(run_id: str, paths: list[str]) -> None:
    async with SessionLocal() as db:
        db.add(
            AuditLog(
                action="restore_failed",
                details=f"run_id={run_id} paths={','.join(paths)}",
            )
        )
        await db.commit()


async def cancel_run(run_id: str) -> bool:
    """Mark a running/pending run as cancelled and terminate its rclone proc.

    Handles the race where cancel arrives after the executor set the DB status
    to running/pending but before it registered the proc in ``active_runs``:
    such a run is still marked cancelled so it can never linger as active."""
    entry = active_runs.get(run_id)
    proc = entry.get("proc") if entry else None
    if proc is not None and proc.returncode is None:
        proc.terminate()
    if entry is not None or await _run_is_active(run_id):
        if entry is not None:
            entry["cancelled"] = True
        await mark_run(run_id, status="cancelled")
        return True
    return False


async def _run_is_active(run_id: str) -> bool:
    async with SessionLocal() as db:
        run = (await db.execute(select(Run).where(Run.run_id == run_id))).scalar_one_or_none()
        return run is not None and run.status in ("pending", "running")
