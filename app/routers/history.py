import math

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import authenticate
from app.dependencies import get_db, templates
from app.models import Run
from app.state import active_runs

router = APIRouter(dependencies=[Depends(authenticate)])

HISTORY_PAGE_SIZE = 25


def _run_filters(kind: str = "", status: str = ""):
    conds = []
    if kind:
        conds.append(Run.kind == kind)
    if status:
        conds.append(Run.status == status)
    return conds


async def _history_context(
    db: AsyncSession, kind: str, status: str, page: int
) -> dict:
    conds = _run_filters(kind, status)
    total = (await db.execute(select(func.count(Run.id)).where(*conds))).scalar_one()
    pages = max(1, math.ceil(total / HISTORY_PAGE_SIZE)) if total else 1
    page = min(max(page, 1), pages)
    offset = (page - 1) * HISTORY_PAGE_SIZE
    runs = (
        await db.execute(
            select(Run).where(*conds).order_by(Run.id.desc())
            .limit(HISTORY_PAGE_SIZE).offset(offset)
        )
    ).scalars().all()
    return {
        "runs": runs,
        "total": total,
        "page": page,
        "pages": pages,
        "kind": kind,
        "status": status,
        "page_size": HISTORY_PAGE_SIZE,
        "show_pagination": True,
        "pagination_url": "/history/partial",
        "filters_id": "history-filters",
        "table_id": "run-table",
    }


@router.get("/history")
async def history_page(
    request: Request,
    db: AsyncSession = Depends(get_db),
    kind: str = "",
    status: str = "",
    page: int = Query(1, ge=1),
):
    ctx = await _history_context(db, kind, status, page)
    return templates.TemplateResponse(
        request,
        "history.html",
        {"active": active_runs, **ctx},
    )


@router.get("/history/partial")
async def history_partial(
    request: Request,
    db: AsyncSession = Depends(get_db),
    kind: str = "",
    status: str = "",
    page: int = Query(1, ge=1),
):
    ctx = await _history_context(db, kind, status, page)
    return templates.TemplateResponse(
        request,
        "components/run_table.html",
        {"active": active_runs, **ctx},
    )
