import asyncio
import json
from pathlib import Path
import re

from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import AuditContext, audit_context
from app.auth import authenticate
from app.database import SessionLocal
from app.dependencies import get_db, templates
from app.models import Remote, Run, target_str
from app.rclone import (
    _clean_rclone_err,
    archive_dates,
    list_archive_tree,
    list_remotes,
    tail,
)
from app.scheduler import cancel_run, create_run
from app.services.restore import default_restore_target
from app.state import active_runs, restore_jobs
from app.utils.path_utils import timestamp_now

router = APIRouter(dependencies=[Depends(authenticate)])

_RESTORE_PATH_RE = re.compile(r"^[a-zA-Z0-9._/-]+$")


def _validate_archive_path(value: str) -> str:
    """Reject path traversal / malformed paths in restore selections."""
    v = (value or "").strip("/")
    if not _RESTORE_PATH_RE.match(v):
        raise HTTPException(
            status_code=400,
            detail="invalid path: only letters, digits, '.', '_', '-', '/' are allowed",
        )
    if any(seg in ("", ".", "..") for seg in v.split("/")):
        raise HTTPException(
            status_code=400, detail="invalid path: '.', '..' and empty segments are not allowed"
        )
    return v


@router.get("/restore")
async def restore_page(request: Request):
    try:
        remotes = await list_remotes()
    except Exception as exc:  # noqa: BLE001
        remotes = []
        error = str(exc)
        return templates.TemplateResponse(
            request, "restore.html", {"remotes": remotes, "error": error}
        )
    return templates.TemplateResponse(
        request, "restore.html", {"remotes": remotes, "error": ""}
    )


@router.get("/restore/archives")
async def restore_archives(request: Request, remote: str, bucket: str = ""):
    try:
        dates = ["latest", *await archive_dates(remote, bucket)]
        error = ""
    except Exception as exc:  # noqa: BLE001
        dates = []
        error = str(exc)
    return templates.TemplateResponse(
        request,
        "components/archive_dates.html",
        {"remote": remote, "bucket": bucket, "dates": dates, "error": error},
    )


@router.get("/restore/tree")
async def archive_tree(
    request: Request, remote: str, bucket: str = "", date: str = "", path: str = ""
):
    path = _validate_archive_path(path) if path else ""
    if date == "latest":
        base = f"{bucket}/latest".rstrip("/")
        empty_hint = "No current /latest/ baseline found (no backup has run yet)"
    else:
        base = f"{bucket}/archive/{date}".rstrip("/")
        empty_hint = f"No archive found for {date} (was it cleaned up by retention?)"
    full = f"{base}/{path}".rstrip("/") if path else base
    try:
        items = await list_archive_tree(remote, full)
        error = ""
        if not items and not path:
            error = empty_hint
    except Exception as exc:  # noqa: BLE001
        items = []
        err = _clean_rclone_err(str(exc))
        if "not found" in err.lower():
            error = empty_hint
        else:
            error = f"Failed to list archive: {err or str(exc)}"
    return templates.TemplateResponse(
        request,
        "components/archive_tree.html",
        {
            "remote": remote,
            "bucket": bucket,
            "date": date,
            "path": path,
            "items": items,
            "error": error,
        },
    )


async def _require_remote(db: AsyncSession, name: str) -> None:
    remote = (
        await db.execute(select(Remote).where(Remote.name == name))
    ).scalar_one_or_none()
    if remote is None:
        raise HTTPException(status_code=400, detail=f"remote '{name}' does not exist")


@router.post("/restore")
async def start_restore(
    request: Request,
    remote: str = Form(...),
    bucket: str = Form(""),
    date: str = Form(...),
    mode: str = Form("files"),
    paths: str = Form(""),
    dest_remote: str = Form(...),
    dest_bucket: str = Form(""),
    dest_path: str = Form(""),
    db: AsyncSession = Depends(get_db),
    audit: AuditContext = Depends(audit_context),
):
    remote = remote.strip()
    dest_remote = dest_remote.strip()
    date = date.strip()
    await _require_remote(db, remote)
    await _require_remote(db, dest_remote)
    if not date:
        raise HTTPException(status_code=400, detail="archive date is required")

    mode = mode if mode == "all" else "files"
    selected = []
    for line in (paths or "").splitlines():
        line = line.strip()
        if not line:
            continue
        kind, _, p = line.partition(":")
        selected.append({"path": _validate_archive_path(p), "is_dir": kind == "d"})
    if mode == "files" and not selected:
        raise HTTPException(status_code=400, detail="select at least one file/folder or restore the whole archive")

    snap = "latest" if date == "latest" else f"archive/{date}"
    archive = target_str(remote, bucket, snap)
    dest_bucket = (dest_bucket or "").strip("/")
    dest_path = (dest_path or "").strip("/")
    if not dest_bucket and not dest_path:
        # Restore engine default target: <source_bucket>-restored-<stamp>.
        dest_path = default_restore_target(bucket, timestamp_now() if date == "latest" else date)
    destination = target_str(dest_remote, dest_bucket, dest_path)

    run_id = await create_run(
        archive, destination, kind="restore", triggered_by="manual"
    )
    restore_jobs[run_id] = {"paths": selected, "mode": mode}
    await audit.log(
        action="restore_triggered",
        details=(
            f"date={date} files={','.join(p['path'] for p in selected) if mode == 'files' else 'ALL'} "
            f"target={destination} run_id={run_id}"
        ),
    )
    return templates.TemplateResponse(
        request,
        "components/restore_started.html",
        {
            "run_id": run_id,
            "archive": archive,
            "destination": destination,
            "mode": mode,
        },
    )


@router.get("/runs/{run_id}/status")
async def run_status(request: Request, run_id: str, db: AsyncSession = Depends(get_db)):
    run = (
        await db.execute(select(Run).where(Run.run_id == run_id))
    ).scalar_one_or_none()
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    progress = None
    if run.status == "running" and run.log_path:
        log_path = Path(run.log_path)
        if log_path.exists():
            progress = _progress_percent(tail(log_path))
    return templates.TemplateResponse(
        request,
        "components/run_status.html",
        {"run": run, "active": active_runs, "progress": progress},
    )


_STATS_PERCENT_RE = re.compile(
    r"[\d.]+\s+\S+\s*/\s*[\d.]+\s+\S+,\s*(\d+)%"
)


def _progress_percent(log_text: str) -> int | None:
    value = None
    for line in log_text.splitlines():
        m = _STATS_PERCENT_RE.search(line)
        if m:
            try:
                value = int(m.group(1))
            except ValueError:
                pass
    return value


@router.get("/runs/{run_id}/log")
async def run_log(request: Request, run_id: str, db: AsyncSession = Depends(get_db)):
    run = (
        await db.execute(select(Run).where(Run.run_id == run_id))
    ).scalar_one_or_none()
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    content = tail(Path(run.log_path)) if run.log_path else "(no log file)"
    return templates.TemplateResponse(
        request, "run_log.html", {"run": run, "content": content}
    )


@router.get("/runs/{run_id}/log/partial")
async def run_log_partial(request: Request, run_id: str, db: AsyncSession = Depends(get_db)):
    run = (
        await db.execute(select(Run).where(Run.run_id == run_id))
    ).scalar_one_or_none()
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    content = tail(Path(run.log_path)) if run.log_path else "(no log file)"
    return templates.TemplateResponse(
        request,
        "components/log.html",
        {"run": run, "content": content, "active": active_runs},
    )


@router.get("/runs/{run_id}/log/stream")
async def run_log_stream(request: Request, run_id: str, db: AsyncSession = Depends(get_db)):
    run = (
        await db.execute(select(Run).where(Run.run_id == run_id))
    ).scalar_one_or_none()
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    log_path = Path(run.log_path) if run.log_path else None

    async def event_gen():
        offset = 0
        while True:
            if log_path and log_path.exists():
                try:
                    with open(log_path, "rb") as f:
                        f.seek(offset)
                        chunk = f.read()
                        offset += len(chunk)
                    if chunk:
                        for line in chunk.decode("utf-8", "replace").splitlines():
                            yield f"data: {json.dumps({'line': line})}\n\n"
                except OSError:
                    pass
            async with SessionLocal() as session:
                current = (
                    await session.execute(select(Run).where(Run.run_id == run_id))
                ).scalar_one_or_none()
                status = current.status if current else "cancelled"
            yield f"data: {json.dumps({'status': status})}\n\n"
            if status in ("success", "failed", "cancelled"):
                yield "event: done\ndata: {}\n\n"
                return
            if await request.is_disconnected():
                return
            await asyncio.sleep(1)

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/runs/{run_id}/cancel")
async def cancel(
    run_id: str,
    db: AsyncSession = Depends(get_db),
    audit: AuditContext = Depends(audit_context),
):
    run = (
        await db.execute(select(Run).where(Run.run_id == run_id))
    ).scalar_one_or_none()
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    cancelled = await cancel_run(run_id)
    if cancelled:
        await audit.log(action="cancel_run", details=f"run_id={run_id}")
    return Response(status_code=204)