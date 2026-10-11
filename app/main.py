from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, select, text

from app.auth import AuthRequired, login_redirect
from app.config import APP_VERSION, settings
from app.database import SessionLocal, engine, init_db
from app.models import Job, Remote, Run
from app.routers import api, auth, dashboard, history, jobs, remotes, restore, transfers
from app.routers.settings import router as settings_router
from app.scheduler import (
    prune_old_logs,
    reconcile_runs_on_startup,
    reschedule_self_backup,
    scheduler,
    start_scheduler,
    stop_scheduler,
)
from app.providers import load_catalogue
from app.runtime_settings import load_db_overrides


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    await load_db_overrides()
    await load_catalogue()
    await reconcile_runs_on_startup()
    await prune_old_logs()
    await start_scheduler()
    yield
    stop_scheduler()


app = FastAPI(title=settings.app_name, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(settings.static_dir)), name="static")


@app.exception_handler(AuthRequired)
async def auth_required_handler(request: Request, exc: AuthRequired):
    return login_redirect(exc.request)


@app.get("/health", include_in_schema=False)
async def health():
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        db_status = "ok"
    except Exception:  # noqa: BLE001
        db_status = "error"
    sched_status = "running" if scheduler.running else "stopped"
    healthy = db_status == "ok" and sched_status == "running"
    return JSONResponse(
        status_code=200 if healthy else 503,
        content={
            "status": "healthy" if healthy else "degraded",
            "db": db_status,
            "scheduler": sched_status,
            "version": APP_VERSION,
        },
    )


@app.get("/metrics", include_in_schema=False)
async def metrics():
    """Prometheus text-format counters for basic monitoring (unauthenticated, no paths/names)."""
    async with SessionLocal() as db:
        total_jobs = (await db.execute(select(func.count(Job.id)))).scalar_one()
        total_remotes = (await db.execute(select(func.count(Remote.id)))).scalar_one()
        rows = (
            await db.execute(
                select(Run.status, func.count(Run.id)).group_by(Run.status)
            )
        ).all()
        storage = (
            await db.execute(
                select(func.coalesce(func.sum(Run.size_bytes), 0)).where(Run.status == "success")
            )
        ).scalar_one()
        last = (
            await db.execute(
                select(func.max(Run.finished_at)).where(Run.status == "success")
            )
        ).scalar_one()
    lines = [
        "# HELP garage_backup_up Whether the orchestrator is up.",
        "# TYPE garage_backup_up gauge",
        "garage_backup_up 1",
        "# HELP garage_backup_jobs_total Number of configured jobs.",
        "# TYPE garage_backup_jobs_total gauge",
        f"garage_backup_jobs_total {total_jobs}",
        "# HELP garage_backup_remotes_total Number of configured remotes.",
        "# TYPE garage_backup_remotes_total gauge",
        f"garage_backup_remotes_total {total_remotes}",
        "# HELP garage_backup_runs_total Number of runs by status.",
        "# TYPE garage_backup_runs_total gauge",
    ]
    for status, count in rows:
        lines.append(f'garage_backup_runs_total{{status="{status}"}} {count}')
    lines.extend(
        [
            "# HELP garage_backup_storage_bytes Bytes backed up by successful runs.",
            "# TYPE garage_backup_storage_bytes gauge",
            f"garage_backup_storage_bytes {storage}",
            "# HELP garage_backup_last_success_timestamp_seconds Last successful run finish time.",
            "# TYPE garage_backup_last_success_timestamp_seconds gauge",
            f"garage_backup_last_success_timestamp_seconds {last.timestamp() if last else 0}",
        ]
    )
    return PlainTextResponse("\n".join(lines) + "\n")


app.include_router(dashboard.router)
app.include_router(jobs.router)
app.include_router(restore.router)
app.include_router(history.router)
app.include_router(remotes.router)
app.include_router(transfers.router)
app.include_router(api.router)
app.include_router(settings_router)
app.include_router(auth.router)
