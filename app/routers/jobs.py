from fastapi import APIRouter, Depends, Form, HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bucket_choices import bucket_choices
from app.audit import AuditContext, audit_context
from app.auth import authenticate
from app.dependencies import get_db, templates
from app.models import Job, Remote, Run, target_str
from app.rclone import list_buckets, list_remotes
from app.scheduler import (
    build_cron_trigger,
    create_run,
    latest_destination,
    schedule_job,
    unschedule_job,
)
from app.schemas import validate_cron_schedule
from app.state import active_runs

router = APIRouter(dependencies=[Depends(authenticate)])


def parse_cron(schedule: str) -> dict:
    parts = (schedule or "").split()
    if len(parts) == 5:
        return {
            "min": parts[0],
            "hour": parts[1],
            "day": parts[2],
            "month": parts[3],
            "dow": parts[4],
        }
    return {"min": "0", "hour": "2", "day": "*", "month": "*", "dow": "*"}


async def _job_list(db: AsyncSession) -> list[dict]:
    jobs = (await db.execute(select(Job).order_by(Job.name))).scalars().all()
    if not jobs:
        return []
    # Two queries total (latest run id per job, then those rows) instead of a
    # per-job N+1 lookup that slows down the jobs table as history grows.
    job_ids = [job.id for job in jobs]
    latest_ids = (
        await db.execute(
            select(func.max(Run.id))
            .where(Run.job_id.in_(job_ids))
            .group_by(Run.job_id)
        )
    ).scalars().all()
    last_runs: dict[int, Run] = {}
    if latest_ids:
        rows = (
            await db.execute(select(Run).where(Run.id.in_(latest_ids)))
        ).scalars().all()
        last_runs = {row.job_id: row for row in rows}
    return [{"job": job, "last_run": last_runs.get(job.id)} for job in jobs]


async def _remotes_safe() -> tuple[list[str], str]:
    try:
        return await list_remotes(), ""
    except Exception as exc:  # noqa: BLE001
        return [], str(exc)


async def _buckets_safe(remote: str) -> tuple[list[str], str]:
    try:
        return await list_buckets(remote), ""
    except Exception as exc:  # noqa: BLE001
        return [], str(exc)


async def _require_remote(db: AsyncSession, name: str) -> None:
    remote = (
        await db.execute(select(Remote).where(Remote.name == name))
    ).scalar_one_or_none()
    if remote is None:
        raise HTTPException(
            status_code=400, detail=f"remote '{name}' does not exist"
        )


async def _create_or_update_job(
    db: AsyncSession,
    job: Job,
    name: str,
    source_remote: str,
    source_bucket: str,
    source_path: str,
    dest_remote: str,
    dest_bucket: str,
    dest_path: str,
    schedule_cron: str,
    enabled: str,
) -> None:
    name = name.strip()
    source_remote = source_remote.strip()
    dest_remote = dest_remote.strip()
    schedule_cron = (schedule_cron or "").strip()

    try:
        schedule_cron = validate_cron_schedule(schedule_cron)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if schedule_cron and build_cron_trigger(schedule_cron) is None:
        raise HTTPException(status_code=400, detail="invalid cron schedule")
    await _require_remote(db, source_remote)
    await _require_remote(db, dest_remote)

    source = target_str(source_remote, source_bucket, source_path)
    destination = target_str(dest_remote, dest_bucket, dest_path)
    if source == destination:
        raise HTTPException(
            status_code=400, detail="source and destination must be different"
        )

    existing = (
        await db.execute(select(Job).where(Job.name == name, Job.id != job.id))
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status_code=409, detail=f"job '{name}' already exists")

    job.name = name
    job.source_remote = source_remote
    job.source_bucket = (source_bucket or "").strip("/")
    job.source_path = (source_path or "").strip("/")
    job.dest_remote = dest_remote
    job.dest_bucket = (dest_bucket or "").strip("/")
    job.dest_path = (dest_path or "").strip("/")
    job.schedule = schedule_cron
    job.enabled = enabled == "true"


@router.get("/jobs")
async def jobs_page(request: Request, db: AsyncSession = Depends(get_db)):
    items = await _job_list(db)
    return templates.TemplateResponse(request, "jobs.html", {"items": items})


@router.get("/jobs/table")
async def jobs_table(request: Request, db: AsyncSession = Depends(get_db)):
    items = await _job_list(db)
    return templates.TemplateResponse(request, "components/job_table.html", {"items": items})


@router.get("/jobs/new")
async def job_form(request: Request):
    remotes, remotes_error = await _remotes_safe()
    return templates.TemplateResponse(
        request,
        "modals/job_form.html",
        {
            "job": None,
            "remotes": remotes,
            "remotes_error": remotes_error,
            "source_buckets": [],
            "dest_buckets": [],
            "cron": parse_cron(""),
            "cron_raw": "",
        },
    )


@router.get("/jobs/{job_id}/edit")
async def job_edit_form(request: Request, job_id: int, db: AsyncSession = Depends(get_db)):
    job = await db.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    remotes, remotes_error = await _remotes_safe()
    source_buckets, _ = await _buckets_safe(job.source_remote)
    dest_buckets, _ = await _buckets_safe(job.dest_remote)
    return templates.TemplateResponse(
        request,
        "modals/job_form.html",
        {
            "job": job,
            "remotes": remotes,
            "remotes_error": remotes_error,
            "source_buckets": source_buckets,
            "dest_buckets": dest_buckets,
            "cron": parse_cron(job.schedule),
            "cron_raw": job.schedule or "",
        },
    )


@router.post("/jobs")
async def create_job(
    request: Request,
    name: str = Form(...),
    source_remote: str = Form(...),
    source_bucket: str = Form(""),
    source_path: str = Form(""),
    dest_remote: str = Form(...),
    dest_bucket: str = Form(""),
    dest_path: str = Form(""),
    schedule_cron: str = Form(""),
    enabled: str = Form("false"),
    db: AsyncSession = Depends(get_db),
    audit: AuditContext = Depends(audit_context),
):
    job = Job()
    await _create_or_update_job(
        db,
        job,
        name,
        source_remote,
        source_bucket,
        source_path,
        dest_remote,
        dest_bucket,
        dest_path,
        schedule_cron,
        enabled,
    )
    db.add(job)
    await db.commit()
    await db.refresh(job)
    if job.enabled:
        schedule_job(job.id, job.schedule)
    await audit.log(action="create_job", details=f"job={job.name} id={job.id}")
    items = await _job_list(db)
    return templates.TemplateResponse(
        request, "components/job_table.html", {"items": items}
    )


@router.post("/jobs/{job_id}")
async def update_job(
    request: Request,
    job_id: int,
    name: str = Form(...),
    source_remote: str = Form(...),
    source_bucket: str = Form(""),
    source_path: str = Form(""),
    dest_remote: str = Form(...),
    dest_bucket: str = Form(""),
    dest_path: str = Form(""),
    schedule_cron: str = Form(""),
    enabled: str = Form("false"),
    db: AsyncSession = Depends(get_db),
    audit: AuditContext = Depends(audit_context),
):
    job = await db.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    await _create_or_update_job(
        db,
        job,
        name,
        source_remote,
        source_bucket,
        source_path,
        dest_remote,
        dest_bucket,
        dest_path,
        schedule_cron,
        enabled,
    )
    await db.commit()
    unschedule_job(job.id)
    if job.enabled:
        schedule_job(job.id, job.schedule)
    await audit.log(action="update_job", details=f"job={job.name} id={job.id}")
    items = await _job_list(db)
    return templates.TemplateResponse(
        request, "components/job_table.html", {"items": items}
    )


@router.post("/jobs/{job_id}/delete")
async def delete_job(
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
    items = await _job_list(db)
    return templates.TemplateResponse(
        audit.request, "components/job_table.html", {"items": items}
    )


@router.post("/jobs/{job_id}/run")
async def run_job(
    job_id: int,
    request: Request,
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
    # htmx sends HX-Target. job_detail.html's "Run Now" targets #runs and needs
    # the runs table back; jobs.html's "Run" targets #jobs-table and needs the
    # job table back. This always returned job_table.html, so "Run Now" injected
    # an entire jobs table into the runs container until #runs' own 3s poll
    # happened to overwrite it.
    if request.headers.get("HX-Target") == "#runs":
        runs = (
            await db.execute(
                select(Run)
                .where(Run.job_id == job_id)
                .order_by(Run.id.desc())
                .limit(50)
            )
        ).scalars().all()
        return templates.TemplateResponse(
            audit.request,
            "components/run_table.html",
            {"runs": runs, "active": active_runs},
        )
    items = await _job_list(db)
    return templates.TemplateResponse(
        audit.request, "components/job_table.html", {"items": items}
    )


@router.post("/jobs/{job_id}/toggle")
async def toggle_job(
    request: Request,
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
    await audit.log(
        action="toggle_job",
        details=f"job={job.name} enabled={job.enabled} id={job_id}",
    )
    items = await _job_list(db)
    return templates.TemplateResponse(
        request, "components/job_table.html", {"items": items}
    )


@router.get("/jobs/{job_id}")
async def job_detail(request: Request, job_id: int, db: AsyncSession = Depends(get_db)):
    job = await db.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    runs = (
        await db.execute(
            select(Run).where(Run.job_id == job_id).order_by(Run.id.desc()).limit(50)
        )
    ).scalars().all()
    return templates.TemplateResponse(
        request,
        "job_detail.html",
        {"job": job, "runs": runs, "active": active_runs},
    )


@router.get("/jobs/{job_id}/runs")
async def job_runs_partial(request: Request, job_id: int, db: AsyncSession = Depends(get_db)):
    runs = (
        await db.execute(
            select(Run).where(Run.job_id == job_id).order_by(Run.id.desc()).limit(50)
        )
    ).scalars().all()
    return templates.TemplateResponse(
        request, "components/run_table.html", {"runs": runs, "active": active_runs}
    )


@router.get("/remotes/buckets")
async def bucket_options(
    request: Request,
    remote: str = "",
    source_remote: str = "",
    dest_remote: str = "",
):
    remote = (remote or source_remote or dest_remote or "").strip()
    buckets, note, error = await bucket_choices(remote)
    return templates.TemplateResponse(
        request,
        "components/bucket_options.html",
        {"buckets": buckets, "note": note, "error": error},
    )