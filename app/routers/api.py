"""JSON REST API (spec v2.0 §6). Complements the HTML partials."""
import asyncio
import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import AuditContext, audit_context
from app.auth import authenticate
from app.database import SessionLocal
from app.dependencies import get_db
from app.models import AuditLog, Job, Remote, Run, target_str
from app.rclone import (
    _quick_flags,
    _resolve_target,
    _run_rclone,
    archive_dates,
    list_archive_tree,
    list_buckets,
    tail,
    write_temp_config,
)
from app.scheduler import (
    build_cron_trigger,
    cancel_run,
    create_run,
    latest_destination,
    schedule_job,
    unschedule_job,
)
from app.operations import (
    OPERATIONS,
    FlagError,
    flags_for_operation,
    list_operations,
    validate_flags,
)
from app.presets import presets_payload
from app.progress import empty_progress
from app.providers import get_provider, load_catalogue, providers_cached, reset_cache
from app.rclone import list_entries, remote_about
from app.routers.transfers import build_target, start_transfer
from app.schemas import validate_cron_schedule
from app.services.restore import default_restore_target
from app.state import active_runs, restore_jobs, run_progress
from app.utils.path_utils import timestamp_now

router = APIRouter(prefix="/api", tags=["api"], dependencies=[Depends(authenticate)])

_STATS_RE = re.compile(
    r"([\d.]+\s+\S+)\s*/\s*([\d.]+\s+\S+),\s*(\d+)%,\s*([\d.]+\s+\S+),\s*ETA\s+(\S+)"
)
_LOG_LINE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s+(\w+)\s*:\s?(.*)$")
_RESTORE_PATH_RE = re.compile(r"^[a-zA-Z0-9._/-]+$")


def _human_bytes(size: int) -> str:
    value = float(size or 0)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{int(value)} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TiB"


def _run_to_dict(run: Run) -> dict:
    return {
        "id": run.id,
        "run_id": run.run_id,
        "job_id": run.job_id,
        "kind": run.kind,
        "operation": run.operation,
        "flags": json.loads(run.flags or "{}"),
        "status": run.status,
        "source": run.source,
        "destination": run.destination,
        "triggered_by": run.triggered_by,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "exit_code": run.exit_code,
        "bytes_transferred": run.size_bytes,
        "files_transferred": run.files,
        "archive_path": run.archive_path or "",
        "log_path": run.log_path,
        "error": run.error,
    }


def _job_to_dict(job: Job, last_run: Run | None = None) -> dict:
    return {
        "id": job.id,
        "name": job.name,
        "source_remote": job.source_remote,
        "source_bucket": job.source_bucket,
        "source_path": job.source_path,
        "dest_remote": job.dest_remote,
        "dest_bucket": job.dest_bucket,
        "dest_path": job.dest_path,
        "schedule_cron": job.schedule or "",
        "enabled": job.enabled,
        "created_at": job.created_at.isoformat(),
        "updated_at": job.updated_at.isoformat() if job.updated_at else None,
        "last_run": (
            {
                "status": last_run.status,
                "started_at": last_run.started_at.isoformat() if last_run.started_at else None,
            }
            if last_run
            else None
        ),
    }


async def _require_remote(db: AsyncSession, name: str) -> None:
    remote = (await db.execute(select(Remote).where(Remote.name == name))).scalar_one_or_none()
    if remote is None:
        raise HTTPException(status_code=400, detail=f"remote '{name}' does not exist")


async def _last_run(db: AsyncSession, job_id: int) -> Run | None:
    return (
        await db.execute(
            select(Run).where(Run.job_id == job_id).order_by(Run.id.desc()).limit(1)
        )
    ).scalar_one_or_none()


# ---------------------------------------------------------------- jobs
@router.get("/jobs")
async def api_jobs(db: AsyncSession = Depends(get_db)):
    jobs = (await db.execute(select(Job).order_by(Job.name))).scalars().all()
    items = []
    for job in jobs:
        items.append(_job_to_dict(job, await _last_run(db, job.id)))
    return {"jobs": items}


@router.post("/jobs", status_code=201)
async def api_create_job(
    payload: dict,
    db: AsyncSession = Depends(get_db),
    audit: AuditContext = Depends(audit_context),
):
    name = (payload.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    source_remote = (payload.get("source_remote") or "").strip()
    dest_remote = (payload.get("dest_remote") or "").strip()
    schedule_cron = (payload.get("schedule_cron") or "").strip()
    try:
        schedule_cron = validate_cron_schedule(schedule_cron)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if schedule_cron and build_cron_trigger(schedule_cron) is None:
        raise HTTPException(status_code=400, detail="invalid cron schedule")
    await _require_remote(db, source_remote)
    await _require_remote(db, dest_remote)
    source = target_str(source_remote, payload.get("source_bucket", ""), payload.get("source_path", ""))
    destination = target_str(dest_remote, payload.get("dest_bucket", ""), payload.get("dest_path", ""))
    if source == destination:
        raise HTTPException(status_code=400, detail="Source and destination must be different")
    existing = (await db.execute(select(Job).where(Job.name == name))).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status_code=409, detail=f"Job '{name}' already exists")

    job = Job(
        name=name,
        source_remote=source_remote,
        source_bucket=(payload.get("source_bucket") or "").strip("/"),
        source_path=(payload.get("source_path") or "").strip("/"),
        dest_remote=dest_remote,
        dest_bucket=(payload.get("dest_bucket") or "").strip("/"),
        dest_path=(payload.get("dest_path") or "").strip("/"),
        schedule=schedule_cron,
        enabled=bool(payload.get("enabled", True)),
    )
    db.add(job)
    await db.commit()
    await db.refresh(job)
    if job.enabled:
        schedule_job(job.id, job.schedule)
    await audit.log(action="create_job", details=f"job={job.name} id={job.id}")
    return _job_to_dict(job, await _last_run(db, job.id))


@router.get("/jobs/{job_id}")
async def api_job_detail(job_id: int, db: AsyncSession = Depends(get_db)):
    """One job plus its last run (the API previously had no single-job route)."""
    job = await db.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return _job_to_dict(job, await _last_run(db, job.id))


@router.put("/jobs/{job_id}")
async def api_update_job(
    job_id: int,
    payload: dict,
    db: AsyncSession = Depends(get_db),
    audit: AuditContext = Depends(audit_context),
):
    """Update a job from JSON (the API could only create/delete jobs)."""
    job = await db.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")

    name = (payload.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    source_remote = (payload.get("source_remote") or "").strip()
    dest_remote = (payload.get("dest_remote") or "").strip()
    schedule_cron = (payload.get("schedule_cron") or "").strip()
    try:
        schedule_cron = validate_cron_schedule(schedule_cron)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if schedule_cron and build_cron_trigger(schedule_cron) is None:
        raise HTTPException(status_code=400, detail="invalid cron schedule")
    await _require_remote(db, source_remote)
    await _require_remote(db, dest_remote)

    source_bucket = (payload.get("source_bucket") or "").strip("/")
    source_path = (payload.get("source_path") or "").strip("/")
    dest_bucket = (payload.get("dest_bucket") or "").strip("/")
    dest_path = (payload.get("dest_path") or "").strip("/")
    if target_str(source_remote, source_bucket, source_path) == target_str(
        dest_remote, dest_bucket, dest_path
    ):
        raise HTTPException(
            status_code=400, detail="Source and destination must be different"
        )
    existing = (
        await db.execute(select(Job).where(Job.name == name, Job.id != job_id))
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status_code=409, detail=f"Job '{name}' already exists")

    job.name = name
    job.source_remote, job.source_bucket, job.source_path = (
        source_remote,
        source_bucket,
        source_path,
    )
    job.dest_remote, job.dest_bucket, job.dest_path = (
        dest_remote,
        dest_bucket,
        dest_path,
    )
    job.schedule = schedule_cron
    job.enabled = bool(payload.get("enabled", job.enabled))
    await db.commit()
    # Re-register the trigger so a new/changed schedule takes effect at once.
    unschedule_job(job.id)
    if job.enabled:
        schedule_job(job.id, job.schedule)
    await audit.log(action="update_job", details=f"job={job.name} id={job.id}")
    return _job_to_dict(job, await _last_run(db, job.id))


@router.post("/jobs/{job_id}/run", status_code=202)
async def api_run_job(
    job_id: int,
    db: AsyncSession = Depends(get_db),
    audit: AuditContext = Depends(audit_context),
):
    job = await db.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    run_id = await create_run(
        target_str(job.source_remote, job.source_bucket, job.source_path),
        latest_destination(job.dest_remote, job.dest_bucket, job.dest_path),
        kind="backup",
        job_id=job.id,
        triggered_by="manual",
    )
    await audit.log(action="run_job", details=f"job={job.name} run_id={run_id}")
    return {"run_id": run_id, "status": "pending"}


@router.post("/jobs/{job_id}/toggle")
async def api_toggle_job(
    job_id: int,
    db: AsyncSession = Depends(get_db),
    audit: AuditContext = Depends(audit_context),
):
    job = await db.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    job.enabled = not job.enabled
    await db.commit()
    unschedule_job(job.id)
    if job.enabled:
        schedule_job(job.id, job.schedule)
    await audit.log(action="toggle_job", details=f"job={job.name} enabled={job.enabled}")
    return {"enabled": job.enabled}


@router.delete("/jobs/{job_id}", status_code=204)
async def api_delete_job(
    job_id: int,
    db: AsyncSession = Depends(get_db),
    audit: AuditContext = Depends(audit_context),
):
    job = await db.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    unschedule_job(job.id)
    name = job.name
    await db.delete(job)
    await db.commit()
    await audit.log(action="delete_job", details=f"job={name} id={job_id}")


# ---------------------------------------------------------------- runs
@router.get("/runs")
async def api_runs(
    db: AsyncSession = Depends(get_db),
    job_id: int | None = None,
    status: str = "",
    kind: str = "",
    limit: int = Query(20, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    stmt = select(Run)
    count_stmt = select(func.count(Run.id))
    if job_id is not None:
        stmt = stmt.where(Run.job_id == job_id)
        count_stmt = count_stmt.where(Run.job_id == job_id)
    if status:
        stmt = stmt.where(Run.status == status)
        count_stmt = count_stmt.where(Run.status == status)
    if kind:
        stmt = stmt.where(Run.kind == kind)
        count_stmt = count_stmt.where(Run.kind == kind)
    total = (await db.execute(count_stmt)).scalar_one()
    runs = (
        await db.execute(
            stmt.order_by(Run.id.desc()).limit(limit).offset(offset)
        )
    ).scalars().all()
    return {"total": total, "runs": [_run_to_dict(r) for r in runs]}


@router.get("/runs/{run_id}/status")
async def api_run_status(run_id: str, db: AsyncSession = Depends(get_db)):
    run = (
        await db.execute(select(Run).where(Run.run_id == run_id))
    ).scalar_one_or_none()
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    progress = None
    if run.status == "running" and run.log_path:
        progress = _parse_progress(tail(Path(run.log_path)))
    return {"status": run.status, "progress": progress}


@router.get("/runs/{run_id}/logs")
async def api_run_logs(
    run_id: str,
    db: AsyncSession = Depends(get_db),
    tail_lines: int = Query(50, alias="tail", ge=1, le=1000),
):
    run = (
        await db.execute(select(Run).where(Run.run_id == run_id))
    ).scalar_one_or_none()
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    content = tail(Path(run.log_path), lines=tail_lines) if run.log_path else ""
    lines = []
    for raw in content.splitlines():
        m = _LOG_LINE_RE.match(raw)
        if m:
            lines.append({"ts": m.group(1), "level": m.group(2), "message": m.group(3)})
        else:
            lines.append({"ts": None, "level": "", "message": raw})
    return {"lines": lines}


@router.post("/runs/{run_id}/cancel", status_code=204)
async def api_cancel_run(
    run_id: str,
    db: AsyncSession = Depends(get_db),
    audit: AuditContext = Depends(audit_context),
):
    run = (
        await db.execute(select(Run).where(Run.run_id == run_id))
    ).scalar_one_or_none()
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    if await cancel_run(run_id):
        await audit.log(action="cancel_run", details=f"run_id={run_id}")


def _parse_progress(log_text: str) -> dict | None:
    match = None
    for line in log_text.splitlines():
        m = _STATS_RE.search(line)
        if m:
            match = m
    if match is None:
        return None
    try:
        percent = int(match.group(3))
    except ValueError:
        percent = 0
    return {
        "bytes_done": match.group(1),
        "bytes_total": match.group(2),
        "percent": percent,
        "speed": match.group(4),
        "eta": match.group(5),
    }


# ---------------------------------------------------------------- restore
async def _dir_size(remote: str, path: str) -> int | None:
    config_path = await write_temp_config()
    try:
        code, out, _err = await _run_rclone(
            config_path, _quick_flags() + ["size", await _resolve_target(remote, path), "--json"]
        )
        if code != 0:
            return None
        data = json.loads(out)
        return int(data.get("bytes", 0))
    except Exception:  # noqa: BLE001
        return None
    finally:
        try:
            os.unlink(config_path)
        except OSError:
            pass


@router.get("/restore/archives")
async def api_restore_archives(remote: str, bucket: str = ""):
    dates = await archive_dates(remote, bucket)
    paths = [f"{bucket}/latest".rstrip("/")] + [
        f"{bucket}/archive/{d}" for d in dates
    ]
    # Fan out the rclone size queries concurrently instead of one slow
    # sequential call per archive folder.
    sizes = await asyncio.gather(*(_dir_size(remote, p) for p in paths))
    archives = []
    for path, size in zip(paths, sizes):
        date = "latest" if path.endswith("/latest") else path.rsplit("/", 1)[-1]
        archives.append(
            {
                "date": date,
                "path": path.removeprefix(f"{bucket}/"),
                "size_human": _human_bytes(size) if size is not None else None,
            }
        )
    return {"archives": archives}


@router.get("/restore/tree")
async def api_restore_tree(remote: str, bucket: str = "", date: str = "", path: str = ""):
    base = f"{bucket}/latest".rstrip("/") if date == "latest" else f"{bucket}/archive/{date}".rstrip("/")
    full = f"{base}/{path}".rstrip("/")
    try:
        items = await list_archive_tree(remote, full)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "items": [
            {"name": i["name"], "type": "dir" if i["is_dir"] else "file", "size": i["size"]}
            for i in items
        ]
    }


@router.post("/restore", status_code=202)
async def api_start_restore(
    payload: dict,
    db: AsyncSession = Depends(get_db),
    audit: AuditContext = Depends(audit_context),
):
    source_remote = (payload.get("source_remote") or "").strip()
    target_remote = (payload.get("target_remote") or "").strip()
    date = (payload.get("archive_date") or "").strip()
    await _require_remote(db, source_remote)
    await _require_remote(db, target_remote)
    if not date:
        raise HTTPException(status_code=400, detail="archive_date is required")

    mode = payload.get("mode", "files")
    mode = mode if mode == "all" else "files"
    prefix = (payload.get("source_path") or "").strip("/")
    selected = []
    for sel in payload.get("selections") or []:
        if isinstance(sel, str):
            kind, _, p = sel.partition(":")
            p = p.strip("/")
            is_dir = kind == "d"
        else:
            p = (sel.get("path") or "").strip("/")
            is_dir = bool(sel.get("is_dir"))
        if not p:
            continue
        if any(seg in ("", ".", "..") for seg in p.split("/")) or not _RESTORE_PATH_RE.match(p):
            raise HTTPException(
                status_code=400,
                detail="invalid path: only letters, digits, '.', '_', '-', '/' are allowed "
                "and '.', '..' or empty segments are not",
            )
        selected.append({"path": f"{prefix}/{p}".strip("/") if prefix else p, "is_dir": is_dir})
    if mode == "files" and not selected:
        raise HTTPException(
            status_code=400, detail="select at least one file/folder or restore the whole archive"
        )

    snap = "latest" if date == "latest" else f"archive/{date}"
    archive = target_str(source_remote, payload.get("source_bucket", ""), snap)
    target_bucket = (payload.get("target_bucket") or "").strip("/")
    target_path = (payload.get("target_path") or "").strip("/")
    if not target_bucket and not target_path:
        # Restore engine default target: <source_bucket>-restored-<stamp>.
        stamp = timestamp_now() if date == "latest" else date
        target_path = default_restore_target(payload.get("source_bucket", ""), stamp)
    destination = target_str(target_remote, target_bucket, target_path)
    run_id = await create_run(archive, destination, kind="restore", triggered_by="manual")
    restore_jobs[run_id] = {"paths": selected, "mode": mode}
    await audit.log(
        action="restore_triggered",
        details=(
            f"date={date} files={','.join(s['path'] for s in selected) if mode == 'files' else 'ALL'} "
            f"target={destination} run_id={run_id}"
        ),
    )
    return {"run_id": run_id, "status": "pending"}


# ---------------------------------------------------------------- rclone surface
@router.get("/providers")
async def api_providers(refresh: bool = False):
    """Every rclone backend with the options needed to configure it."""
    providers = await load_catalogue(force_refresh=refresh)
    return {
        "count": len(providers),
        "providers": [p.summary() for p in providers],
    }


@router.get("/providers/{name}")
async def api_provider_detail(name: str):
    """Full option list for one backend (drives the dynamic remote form)."""
    await load_catalogue()
    provider = get_provider(name)
    if provider is None:
        raise HTTPException(status_code=404, detail=f"unknown backend '{name}'")
    detail = provider.detail()
    detail.update(presets_payload(provider))
    return detail


@router.post("/providers/refresh")
async def api_providers_refresh():
    reset_cache()
    providers = await load_catalogue(force_refresh=True)
    return {"count": len(providers), "providers": [p.summary() for p in providers]}


@router.get("/operations")
async def api_operations(operation: str = ""):
    """The operation catalogue (+ the flags valid for one operation)."""
    payload = {"operations": list_operations()}
    if operation:
        if operation not in OPERATIONS:
            raise HTTPException(status_code=404, detail=f"unknown operation '{operation}'")
        payload["operation"] = OPERATIONS[operation].summary()
        payload["flags"] = flags_for_operation(operation)
    return payload


@router.get("/browse")
async def api_browse(
    remote: str, path: str = "", dirs_only: bool = False, limit: int = Query(500, ge=1, le=5000)
):
    """List a path on any backend (JSON)."""
    if not remote:
        raise HTTPException(status_code=400, detail="remote is required")
    try:
        items = await list_entries(remote, (path or "").strip("/"), dirs_only=dirs_only)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "remote": remote,
        "path": (path or "").strip("/"),
        "items": [
            {
                "name": i["name"],
                "path": i["path"],
                "type": "dir" if i["is_dir"] else "file",
                "size": i["size"],
            }
            for i in sorted(items, key=lambda i: (not i["is_dir"], i["name"].lower()))[:limit]
        ],
    }


@router.get("/remotes/{name}/about")
async def api_remote_about(name: str, path: str = "", db: AsyncSession = Depends(get_db)):
    """Quota information for a remote, when the backend supports `rclone about`."""
    remote = (
        await db.execute(select(Remote).where(Remote.name == name))
    ).scalar_one_or_none()
    if remote is None:
        raise HTTPException(status_code=400, detail=f"remote '{name}' does not exist")
    about = await remote_about(name, path)
    return {"remote": name, "about": about, "supported": about is not None}


@router.post("/transfers/run", status_code=202)
async def api_run_transfer(
    payload: dict,
    db: AsyncSession = Depends(get_db),
    audit: AuditContext = Depends(audit_context),
):
    """Start any rclone operation (copy, sync, move, delete, check, …).

    Body: {operation, source_remote, source_bucket?, source_path?, dest_remote?,
    dest_bucket?, dest_path?, flags?: {…}}. Paths may also be given directly as
    `source`/`destination` (`remote:bucket/path`).
    """
    operation = (payload.get("operation") or "").strip()
    if not operation:
        raise HTTPException(status_code=400, detail="operation is required")
    source = (payload.get("source") or "").strip() or build_target(
        payload.get("source_remote") or "",
        payload.get("source_bucket") or "",
        payload.get("source_path") or "",
    )
    destination = (payload.get("destination") or "").strip() or build_target(
        payload.get("dest_remote") or "",
        payload.get("dest_bucket") or "",
        payload.get("dest_path") or "",
    )
    try:
        run = await start_transfer(
            db, audit, operation, source, destination, payload.get("flags") or {}
        )
    except FlagError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "run_id": run.run_id,
        "operation": run.operation,
        "status": "pending",
        "source": source,
        "destination": destination,
    }


@router.get("/runs/{run_id}/progress")
async def api_run_progress(run_id: str, db: AsyncSession = Depends(get_db)):
    """Structured progress of a run (live while running, from its log once done)."""
    from app.services.transfer import progress_for

    run = (
        await db.execute(select(Run).where(Run.run_id == run_id))
    ).scalar_one_or_none()
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    progress = progress_for(run_id, run.log_path)
    return {
        "run_id": run_id,
        "status": run.status,
        "operation": run.operation,
        "progress": progress,
    }


# ---------------------------------------------------------------- history / audit
_TARGET_TYPES = {
    "create_job": "job", "update_job": "job", "delete_job": "job", "run_job": "job",
    "toggle_job": "job", "remote_created": "remote", "remote_updated": "remote",
    "remote_deleted": "remote", "restore_triggered": "run", "restore_failed": "run",
    "cancel_run": "run",
}
_ID_RE = re.compile(r"id=(\d+)")
_NAME_RE = re.compile(r"(?:job|remote)=([\w-]+)")


def _audit_to_dict(entry: AuditLog) -> dict:
    details = entry.details or ""
    try:
        parsed = json.loads(details) if details.startswith("{") else None
    except (json.JSONDecodeError, TypeError):
        parsed = None
    m = _ID_RE.search(details)
    n = _NAME_RE.search(details)
    return {
        "id": entry.id,
        "ts": entry.timestamp.isoformat() if entry.timestamp else None,
        "user": entry.username,
        "ip_address": entry.ip_address,
        "method": entry.method,
        "path": entry.path,
        "action": entry.action,
        "target_type": _TARGET_TYPES.get(entry.action),
        "target_id": int(m.group(1)) if m else None,
        "target_name": n.group(1) if n else None,
        "details": parsed if parsed is not None else details,
        "status_code": entry.status_code,
        "success": entry.success,
    }


@router.get("/history")
async def api_history(
    db: AsyncSession = Depends(get_db),
    action: str = "",
    target_type: str = "",
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    stmt = select(AuditLog)
    count_stmt = select(func.count(AuditLog.id))
    if action:
        stmt = stmt.where(AuditLog.action == action)
        count_stmt = count_stmt.where(AuditLog.action == action)
    if target_type:
        actions = [a for a, t in _TARGET_TYPES.items() if t == target_type]
        stmt = stmt.where(AuditLog.action.in_(actions))
        count_stmt = count_stmt.where(AuditLog.action.in_(actions))
    total = (await db.execute(count_stmt)).scalar_one()
    rows = (
        await db.execute(stmt.order_by(AuditLog.id.desc()).limit(limit).offset(offset))
    ).scalars().all()
    return {"total": total, "items": [_audit_to_dict(r) for r in rows]}


@router.get("/history/recent")
async def api_history_recent(db: AsyncSession = Depends(get_db)):
    rows = (
        await db.execute(select(AuditLog).order_by(AuditLog.id.desc()).limit(20))
    ).scalars().all()
    return {"items": [_audit_to_dict(r) for r in rows]}


@router.get("/history/stats")
async def api_history_stats(db: AsyncSession = Depends(get_db)):
    since = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=6)
    rows = (
        await db.execute(
            select(func.date(AuditLog.timestamp), func.count(AuditLog.id))
            .where(AuditLog.timestamp >= since)
            .group_by(func.date(AuditLog.timestamp))
        )
    ).all()
    counts = {str(d): c for d, c in rows}
    days = []
    for i in range(6, -1, -1):
        day = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=i)).date()
        days.append({"date": day.isoformat(), "count": counts.get(day.isoformat(), 0)})
    return {"days": days}


# ---------------------------------------------------------------- dashboard
@router.get("/dashboard/stats")
async def api_dashboard_stats(db: AsyncSession = Depends(get_db)):
    total_jobs = (await db.execute(select(func.count(Job.id)))).scalar_one()
    total_remotes = (await db.execute(select(func.count(Remote.id)))).scalar_one()
    since = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=24)
    runs_24h = (
        await db.execute(
            select(func.count(Run.id)).where(Run.started_at >= since)
        )
    ).scalar_one()
    success_24h = (
        await db.execute(
            select(func.count(Run.id)).where(Run.started_at >= since, Run.status == "success")
        )
    ).scalar_one()
    active_runs_count = (
        await db.execute(select(func.count(Run.id)).where(Run.status.in_(["pending", "running"])))
    ).scalar_one()
    storage_bytes = (
        await db.execute(
            select(func.coalesce(func.sum(Run.size_bytes), 0)).where(Run.status == "success")
        )
    ).scalar_one()
    success_rate = int(round(100 * success_24h / runs_24h)) if runs_24h else 100
    return {
        "total_jobs": total_jobs,
        "total_remotes": total_remotes,
        "runs_24h": runs_24h,
        "success_rate_24h": success_rate,
        "active_runs": active_runs_count,
        "storage_used_human": _human_bytes(storage_bytes),
    }
