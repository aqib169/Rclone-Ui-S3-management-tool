import math

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import authenticate
from app.config import settings
from app.dependencies import get_db, templates
from app.models import AuditLog, Job, Remote, Run
from app.providers import get_provider, load_catalogue, providers_cached
from app.remote_config import options_summary
from app.state import active_runs

router = APIRouter(dependencies=[Depends(authenticate)])

AUDIT_PAGE_SIZE = 25

_AUDIT_ACTIONS = [
    "create_job",
    "update_job",
    "delete_job",
    "run_job",
    "toggle_job",
    "remote_created",
    "remote_updated",
    "remote_deleted",
    "restore_triggered",
    "restore_failed",
    "cancel_run",
    "self_backup",
    "settings_updated",
]


async def _remotes_page_data(db: AsyncSession) -> list[dict]:
    remotes = (
        await db.execute(select(Remote).order_by(Remote.name))
    ).scalars().all()
    jobs = (await db.execute(select(Job.name, Job.source_remote, Job.dest_remote))).all()
    usage: dict[str, list[str]] = {}
    for job_name, src, dst in jobs:
        if src:
            usage.setdefault(src, []).append(job_name)
        if dst:
            usage.setdefault(dst, []).append(job_name)
    return [{"remote": r, "jobs": usage.get(r.name, [])} for r in remotes]


async def _stats(db: AsyncSession) -> dict:
    total_jobs = (await db.execute(select(func.count(Job.id)))).scalar_one()
    enabled_jobs = (
        await db.execute(select(func.count(Job.id)).where(Job.enabled.is_(True)))
    ).scalar_one()
    total_runs = (await db.execute(select(func.count(Run.id)))).scalar_one()
    running = (
        await db.execute(select(func.count(Run.id)).where(Run.status == "running"))
    ).scalar_one()
    success = (
        await db.execute(select(func.count(Run.id)).where(Run.status == "success"))
    ).scalar_one()
    failed = (
        await db.execute(select(func.count(Run.id)).where(Run.status == "failed"))
    ).scalar_one()
    total_size = (
        await db.execute(select(func.coalesce(func.sum(Run.size_bytes), 0)))
    ).scalar_one()
    return {
        "total_jobs": total_jobs,
        "enabled_jobs": enabled_jobs,
        "total_runs": total_runs,
        "running": running,
        "success": success,
        "failed": failed,
        "total_size": total_size or 0,
    }


async def _active_runs(db: AsyncSession) -> list[Run]:
    return (
        await db.execute(
            select(Run)
            .where(Run.status.in_(["pending", "running"]))
            .order_by(Run.started_at.desc(), Run.id.desc())
        )
    ).scalars().all()


def _progress_map(runs: list[Run]) -> dict:
    """run_id -> live progress dict, for the active-runs list.

    Read from the in-memory snapshot first and the run's log tail second (see
    app.services.transfer.progress_for), so scheduler-started runs and runs
    started before a restart still report progress instead of showing 0%.
    Only the running/pending rows are resolved — a finished run needs no bar.
    """
    from app.services.transfer import progress_for

    out: dict = {}
    for run in runs:
        if run.status not in ("pending", "running"):
            continue
        try:
            out[run.run_id] = progress_for(run.run_id, run.log_path)
        except Exception:
            # A broken/missing log must not take down the whole dashboard; the
            # row simply renders without a bar.
            out[run.run_id] = None
    return out


@router.get("/")
async def dashboard(request: Request, db: AsyncSession = Depends(get_db)):
    stats = await _stats(db)
    runs = await _active_runs(db)
    # Progress for the getting-started card: which setup steps are outstanding.
    # A new user otherwise sees three buttons and no idea which to press first,
    # or what any of them mean.
    remote_count = (
        await db.execute(select(func.count()).select_from(Remote))
    ).scalar() or 0
    job_count = (
        await db.execute(select(func.count()).select_from(Job))
    ).scalar() or 0
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "stats": stats,
            "runs": runs,
            "active": active_runs,
            "progress_map": _progress_map(runs),
            "remote_count": remote_count,
            "job_count": job_count,
        },
    )


@router.get("/help")
async def help_page(request: Request):
    return templates.TemplateResponse(
        request, "help.html", {"retain_days": settings.retain_days}
    )


@router.get("/guide")
async def guide_page(request: Request):
    """Interactive user guide: the four core workflows, end to end.

    A static page like /help — it renders the template and needs no database
    access, so it takes no AsyncSession dependency.
    """
    return templates.TemplateResponse(request, "pages/guide.html")


@router.get("/dashboard/stats")
async def stats_partial(request: Request, db: AsyncSession = Depends(get_db)):
    stats = await _stats(db)
    return templates.TemplateResponse(request, "components/stats.html", {"stats": stats})


@router.get("/dashboard/active")
async def active_partial(request: Request, db: AsyncSession = Depends(get_db)):
    runs = await _active_runs(db)
    return templates.TemplateResponse(
        request,
        "components/active_runs.html",
        {
            "runs": runs,
            "active": active_runs,
            "progress_map": _progress_map(runs),
        },
    )


@router.get("/remotes")
async def remotes_page(request: Request, db: AsyncSession = Depends(get_db)):
    items = await _remotes_page_data(db)
    return templates.TemplateResponse(request, "remotes.html", {"items": items})


@router.get("/remotes/table")
async def remotes_table(request: Request, db: AsyncSession = Depends(get_db)):
    items = await _remotes_page_data(db)
    return templates.TemplateResponse(
        request, "components/remote_table.html", {"items": items}
    )


@router.get("/remotes/new")
async def remotes_new_modal(request: Request):
    await load_catalogue()
    return templates.TemplateResponse(
        request,
        "modals/remote_form.html",
        {
            "remote": None,
            "providers": providers_cached(),
            "options": {},
        },
    )


@router.get("/remotes/{remote_id}/edit")
async def remotes_edit_modal(
    request: Request, remote_id: int, db: AsyncSession = Depends(get_db)
):
    remote = await db.get(Remote, remote_id)
    if remote is None:
        raise HTTPException(status_code=404, detail="Remote not found")
    await load_catalogue()
    provider = get_provider(remote.type)
    return templates.TemplateResponse(
        request,
        "modals/remote_form.html",
        {
            "remote": remote,
            "providers": providers_cached(),
            "provider": provider,
            # Only non-secret option values are pre-filled; secrets show as
            # "(unchanged)" so a blank submit keeps the stored value.
            "options": {
                o["name"]: o for o in options_summary(remote) if not o["secret"]
            },
        },
    )


async def _audit_context(
    db: AsyncSession,
    search: str = "",
    action: str = "",
    user: str = "",
    page: int = 1,
) -> dict:
    conds = []
    if action:
        conds.append(AuditLog.action == action)
    if user:
        conds.append(AuditLog.username == user)
    if search:
        like = f"%{search.strip()}%"
        conds.append(
            or_(
                AuditLog.details.like(like),
                AuditLog.path.like(like),
                AuditLog.action.like(like),
                AuditLog.username.like(like),
            )
        )
    total = (await db.execute(select(func.count(AuditLog.id)).where(*conds))).scalar_one()
    pages = max(1, math.ceil(total / AUDIT_PAGE_SIZE)) if total else 1
    page = min(max(page, 1), pages)
    offset = (page - 1) * AUDIT_PAGE_SIZE
    logs = (
        await db.execute(
            select(AuditLog).where(*conds).order_by(AuditLog.id.desc())
            .limit(AUDIT_PAGE_SIZE).offset(offset)
        )
    ).scalars().all()
    return {
        "logs": logs,
        "total": total,
        "page": page,
        "pages": pages,
        "search": search,
        "action": action,
        "user": user,
        "page_size": AUDIT_PAGE_SIZE,
        "show_pagination": True,
        "pagination_url": "/audit/partial",
        "filters_id": "audit-filters",
        "table_id": "audit-table",
    }


@router.get("/audit")
async def audit_page(
    request: Request,
    db: AsyncSession = Depends(get_db),
    search: str = "",
    action: str = "",
    user: str = "",
    page: int = Query(1, ge=1),
):
    ctx = await _audit_context(db, search=search, action=action, user=user, page=page)
    return templates.TemplateResponse(
        request,
        "audit.html",
        {"actions": _AUDIT_ACTIONS, **ctx},
    )


@router.get("/audit/partial")
async def audit_partial(
    request: Request,
    db: AsyncSession = Depends(get_db),
    search: str = "",
    action: str = "",
    user: str = "",
    page: int = Query(1, ge=1),
):
    ctx = await _audit_context(db, search=search, action=action, user=user, page=page)
    return templates.TemplateResponse(
        request,
        "components/audit_table.html",
        ctx,
    )
