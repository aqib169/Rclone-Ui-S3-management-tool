"""Transfer Console — drive any rclone operation visually.

The Jobs page is for *scheduled backups*; this page is for *running rclone now*:
pick an operation, pick a source and a destination (any backend, browsable with
the path browser), set flags, run it and watch real progress.

Everything the page does is also available as JSON under `/api/transfers/*`,
and the operation/flag vocabulary comes from `app/operations.py` so the UI and
the API can never disagree about what rclone accepts.
"""

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bucket_choices import bucket_choices
from app.audit import AuditContext, audit_context
from app.auth import authenticate
from app.dependencies import get_db, templates
from app.models import Remote, Run, target_str
from app.operations import (
    FLAGS,
    FlagError,
    flags_for_operation,
    get_operation,
    list_operations,
    validate_flags,
)
from app.rclone import list_entries, list_remotes, remote_about
from app.scheduler import create_run
from app.services.locks import TargetBusy
from app.services.transfer import progress_for
from app.state import active_runs

router = APIRouter(dependencies=[Depends(authenticate)])

# Categories in the order the picker shows them.
CATEGORY_ORDER = ["Copy & sync", "Move & delete", "Verify", "Maintenance", "Inspect"]

_FLAG_GROUPS_ORDER = (
    "Safety",
    "Filtering",
    "Transfer",
    "Performance",
    "Verify",
    "Inspect",
)


def _group_flags(operation: str) -> list:
    """Flag specs for an operation, grouped for the options panel."""
    groups: dict = {}
    for spec in flags_for_operation(operation):
        if not spec.get("supported"):
            continue
        groups.setdefault(spec["group"], []).append(spec)
    ordered = [(g, groups[g]) for g in _FLAG_GROUPS_ORDER if g in groups]
    ordered += [(g, v) for g, v in groups.items() if g not in _FLAG_GROUPS_ORDER]
    return [{"group": g, "flags": v} for g, v in ordered]


async def _remote_rows(db: AsyncSession) -> list:
    return (await db.execute(select(Remote).order_by(Remote.name))).scalars().all()


async def _require_remote(db: AsyncSession, name: str) -> Remote:
    remote = (
        await db.execute(select(Remote).where(Remote.name == name))
    ).scalar_one_or_none()
    if remote is None:
        raise HTTPException(status_code=400, detail=f"remote '{name}' does not exist")
    return remote


def _collect_flags(form) -> dict:
    """Read `flag_<name>` form fields into a flag map (empty fields ignored)."""
    raw = {}
    for key, value in form.items():
        if not key.startswith("flag_"):
            continue
        name = key[5:]
        if name in FLAGS and str(value).strip():
            raw[name] = value
    return raw


def build_target(remote: str, bucket: str, path: str) -> str:
    """`remote` + optional bucket + optional path -> `remote:bucket/path`."""
    if not remote:
        return ""
    return target_str(remote, (bucket or "").strip("/"), (path or "").strip("/"))


# ---------------------------------------------------------------- pages
@router.get("/transfers")
async def transfers_page(request: Request, db: AsyncSession = Depends(get_db)):
    remotes = await _remote_rows(db)
    return templates.TemplateResponse(
        request,
        "transfers.html",
        {
            "remotes": remotes,
            "operations": list_operations(),
            "categories": CATEGORY_ORDER,
            "active": active_runs,
        },
    )


@router.get("/transfers/form")
async def transfers_form(
    request: Request, operation: str = "copy", db: AsyncSession = Depends(get_db)
):
    """The builder form for one operation (re-rendered when it changes)."""
    spec = get_operation(operation)
    if spec is None:
        raise HTTPException(status_code=404, detail=f"unknown operation '{operation}'")
    return templates.TemplateResponse(
        request,
        "components/transfer_form.html",
        {
            "op": spec,
            "flag_groups": _group_flags(spec.name),
            "remotes": await _remote_rows(db),
        },
    )


@router.get("/transfers/remotes")
async def transfers_remotes(request: Request, role: str = "source"):
    try:
        names = await list_remotes()
    except Exception as exc:  # noqa: BLE001
        return templates.TemplateResponse(
            request,
            "components/remote_options.html",
            {"remotes": [], "role": role, "error": str(exc)},
        )
    return templates.TemplateResponse(
        request,
        "components/remote_options.html",
        {"remotes": names, "role": role, "error": ""},
    )


@router.get("/transfers/buckets")
async def transfers_buckets(
    request: Request,
    remote: str = "",
    source_remote: str = "",
    dest_remote: str = "",
):
    # htmx sends the triggering <select>'s own name, which is source_remote or
    # dest_remote here — not "remote". Accepting only "remote" meant this always
    # got an empty name and the picker always said "Failed to load buckets".
    name = (remote or source_remote or dest_remote or "").strip()
    buckets, note, error = await bucket_choices(name)
    return templates.TemplateResponse(
        request,
        "components/bucket_options.html",
        {"buckets": buckets, "note": note, "error": error},
    )


@router.get("/transfers/listing")
async def transfers_listing(
    request: Request, remote: str = "", path: str = "", dirs_only: bool = True
):
    """Directory listing used by the path browser (works on any backend)."""
    if not remote:
        return templates.TemplateResponse(
            request,
            "components/path_listing.html",
            {
                "items": [],
                "remote": "",
                "path": "",
                "error": "no remote selected",
                "parent": "",
            },
        )
    path = (path or "").strip("/")
    try:
        items = await list_entries(remote, path, dirs_only=dirs_only)
        error = ""
    except Exception as exc:  # noqa: BLE001
        items, error = [], str(exc)
    items = sorted(items, key=lambda i: (not i["is_dir"], i["name"].lower()))
    parent = path.rsplit("/", 1)[0] if "/" in path else ""
    return templates.TemplateResponse(
        request,
        "components/path_listing.html",
        {
            "items": items,
            "remote": remote,
            "path": path,
            "error": error,
            "parent": parent,
        },
    )


@router.get("/transfers/about")
async def transfers_about(request: Request, remote: str = "", path: str = ""):
    about = await remote_about(remote, path) if remote else None
    return templates.TemplateResponse(
        request, "components/remote_about.html", {"about": about, "remote": remote}
    )
async def start_transfer(
    db: AsyncSession,
    audit: AuditContext,
    operation: str,
    source: str,
    destination: str,
    flags: dict,
) -> Run:
    """Validate + launch one ad-hoc operation. Shared by the HTML form and JSON."""
    spec = get_operation(operation)
    if spec is None:
        raise FlagError(f"unknown operation '{operation}'")
    clean = validate_flags(flags, operation, require_confirmed=True)
    source = (source or "").strip()
    destination = (destination or "").strip()
    if spec.needs_source and not source:
        raise FlagError(f"'{operation}' requires a source path")
    if spec.needs_destination and not destination:
        raise FlagError(f"'{operation}' requires a destination path")
    # Fail fast on an unknown remote instead of letting rclone discover it later.
    for target in (source, destination):
        if not target:
            continue
        await _require_remote(db, target.split(":", 1)[0])

    try:
        run_id = await create_run(
            source,
            destination,
            kind="transfer",
            triggered_by="manual",
            operation=operation,
            flags=clean,
        )
    except TargetBusy as busy:
        raise FlagError(
            f"{busy.target} is busy: run {busy.other_run[:8]} is already "
            f"modifying it. Wait for it to finish, or pick another path."
        ) from busy
    await audit.log(
        action="transfer_started",
        details=(
            f"op={operation} source={source} dest={destination or '-'} "
            f"flags={','.join(clean) or '-'} run_id={run_id}"
        ),
    )
    return Run(run_id=run_id, operation=operation)


@router.post("/transfers/run")
async def transfers_run(
    request: Request,
    operation: str = Form(...),
    source_remote: str = Form(""),
    source_bucket: str = Form(""),
    source_path: str = Form(""),
    dest_remote: str = Form(""),
    dest_bucket: str = Form(""),
    dest_path: str = Form(""),
    confirmed: str = Form(""),
    db: AsyncSession = Depends(get_db),
    audit: AuditContext = Depends(audit_context),
):
    flags = _collect_flags(await request.form())
    if confirmed:
        flags["_confirmed"] = True
    source = build_target(source_remote, source_bucket, source_path)
    destination = build_target(dest_remote, dest_bucket, dest_path)
    try:
        run = await start_transfer(
            db, audit, operation, source, destination, flags
        )
    except FlagError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return templates.TemplateResponse(
        request,
        "components/transfer_started.html",
        {
            "run_id": run.run_id,
            "operation": run.operation,
            "source": source,
            "destination": destination,
        },
    )


@router.get("/runs/{run_id}/progress")
async def run_progress_partial(request: Request, run_id: str, db: AsyncSession = Depends(get_db)):
    run = (
        await db.execute(select(Run).where(Run.run_id == run_id))
    ).scalar_one_or_none()
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    progress = progress_for(run_id, run.log_path)
    return templates.TemplateResponse(
        request,
        "components/progress.html",
        {"run": run, "progress": progress, "active": active_runs},
    )