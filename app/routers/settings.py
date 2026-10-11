"""Settings page + JSON API.

Environment variables are the bootstrap default (see config.py / .env). Any
value changed here is stored in the `settings` table and overrides the env
value at runtime — without a rebuild or restart being required. Settings are
re-applied on save and at startup (main.py lifespan).
"""

import re
from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy import select

from app.audit import AuditContext, audit_context
from app.auth import authenticate
from app.config import settings
from app.dependencies import get_db, templates
from app.models import Setting
from app.runtime_settings import (
    OVERRIDABLE_KEYS,
    apply_overrides,
    effective_value,
    env_default,
    load_db_overrides,
    store_setting,
)
from app.scheduler import reschedule_self_backup

router = APIRouter(dependencies=[Depends(authenticate)])

SETTING_FIELDS = [
    {
        "key": "username",
        "label": "Login username",
        "env": "USERNAME",
        "type": "text",
        "placeholder": "admin",
        "section": "Authentication",
        "info": "The login name used to access the web UI and the JSON API (HTTP Basic auth).",
    },
    {
        "key": "password",
        "label": "Password",
        "env": "PASSWORD",
        "type": "password",
        "placeholder": "\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022 (blank = keep current)",
        "section": "Authentication",
        "info": "The login password (minimum 8 characters). Stored encrypted and never shown; leave blank to keep the current password. After changing it, log in again.",
    },
    {
        "key": "self_backup_dest",
        "label": "Self-backup destination",
        "env": "SELF_BACKUP_DEST",
        "type": "text",
        "placeholder": "arc:backup/orchestrator",
        "section": "Operations",
        "info": "Where the orchestrator backs up its own data (config, audit log, run logs) every day at 03:10 UTC. Format remote:bucket/path. Blank disables the self-backup.",
    },
    {
        "key": "retain_days",
        "label": "Archive retention (days)",
        "env": "RETAIN_DAYS",
        "type": "number",
        "min": 1,
        "max": 3650,
        "section": "Retention",
        "info": "How many date-stamped archive folders are kept per enabled job. Older dates are pruned permanently every day at 03:00 UTC.",
    },
    {
        "key": "log_retain_days",
        "label": "Run-log retention (days)",
        "env": "LOG_RETAIN_DAYS",
        "type": "number",
        "min": 1,
        "max": 3650,
        "section": "Retention",
        "info": "How many days of run logs are kept. Old logs are deleted at boot and daily at 03:05 UTC.",
    },
    {
        "key": "rclone_transfers",
        "label": "rclone parallel transfers",
        "env": "RCLONE_TRANSFERS",
        "type": "number",
        "min": 1,
        "max": 16,
        "section": "Performance",
        "info": "Number of files rclone transfers in parallel per backup/restore. Higher values use more bandwidth/CPU; 1\u201316.",
    },
    {
        "key": "stats_interval",
        "label": "rclone stats interval (s)",
        "env": "STATS_INTERVAL",
        "type": "number",
        "min": 1,
        "max": 3600,
        "section": "Performance",
        "info": "Seconds between rclone stats lines. Drives the live progress bar and SSE log updates. Lower values update faster; 1\u20133600.",
    },
]

# Read-only environment variables that are fixed at boot time (not changeable
# from the UI). Shown for reference so operators can see every env var in use.
BOOT_FIELDS = [
    {
        "env": "DATA_DIR",
        "value": str(settings.data_dir),
        "info": "Directory holding the SQLite database, run logs and the encryption key. Set in the container environment.",
    },
    {
        "env": "RCLONE_BIN",
        "value": settings.rclone_bin,
        "info": "Path to the rclone binary used for all transfers, listings and tests.",
    },
    {
        "env": "POLL_INTERVAL",
        "value": str(settings.poll_interval),
        "info": "Seconds between active-run status polls (dashboard / active transfers).",
    },
    {
        "env": "BUSY_TIMEOUT_MS",
        "value": str(settings.busy_timeout_ms),
        "info": "SQLite busy timeout in milliseconds \u2014 avoids \u201cdatabase is locked\u201d errors under concurrent access.",
    },
    {
        "env": "APP_NAME",
        "value": settings.app_name,
        "info": "Application title shown in the browser tab.",
    },
]

_REMOTE_DEST = re.compile(r"^[A-Za-z0-9_-]+:[^\s]+$")


def _field_for(key: str) -> dict:
    return next(f for f in SETTING_FIELDS if f["key"] == key)


def _validated(key: str, value: str) -> str:
    value = (value or "").strip()
    if key in ("username", "password"):
        # blank keeps the current value; the caller decides keep-vs-revert
        if not value:
            return value
        if key == "password" and len(value) < 8:
            raise HTTPException(status_code=422, detail="password must be at least 8 characters")
        if key == "username" and len(value) > 100:
            raise HTTPException(status_code=422, detail="username must be 1-100 characters")
        return value
    # empty = revert to the environment default
    if not value:
        return value
    if key == "self_backup_dest":
        if not _REMOTE_DEST.match(value):
            raise HTTPException(status_code=422, detail="self_backup_dest must look like remote:bucket/path")
        return value
    meta = _field_for(key)
    try:
        num = int(value)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"{key} must be a number") from None
    lo, hi = int(meta["min"]), int(meta["max"])
    if not (lo <= num <= hi):
        raise HTTPException(status_code=422, detail=f"{key} must be between {lo} and {hi}")
    return str(num)


async def _settings_view(db) -> dict[str, Any]:
    rows = (await db.execute(select(Setting))).scalars().all()
    overridden = {row.key for row in rows}
    fields = []
    for f in SETTING_FIELDS:
        key = f["key"]
        is_pw = key == "password"
        effective = "" if is_pw else effective_value(key)
        default = "" if is_pw else env_default(key)
        fields.append(
            {
                **f,
                "value": effective,
                "default": default,
                "overridden": key in overridden,
            }
        )
    groups: list[dict] = []
    order: dict[str, int] = {}
    for f in fields:
        title = f["section"]
        if title not in order:
            order[title] = len(groups)
            groups.append({"title": title, "fields": []})
        groups[order[title]]["fields"].append(f)
    return {
        "fields": fields,
        "overridden": overridden,
        "groups": groups,
        "boot_fields": BOOT_FIELDS,
    }


@router.get("/settings")
async def settings_page(request: Request, db=Depends(get_db)):
    view = await _settings_view(db)
    view["saved"] = bool(request.query_params.get("saved"))
    return templates.TemplateResponse(request, "settings.html", view)


@router.post("/settings")
async def settings_update(
    request: Request,
    username: str = Form(None),
    password: str = Form(None),
    self_backup_dest: str = Form(None),
    retain_days: str = Form(None),
    log_retain_days: str = Form(None),
    rclone_transfers: str = Form(None),
    stats_interval: str = Form(None),
    db=Depends(get_db),
    audit: AuditContext = Depends(audit_context),
):
    form_map = {
        "username": username,
        "password": password,
        "self_backup_dest": self_backup_dest,
        "retain_days": retain_days,
        "log_retain_days": log_retain_days,
        "rclone_transfers": rclone_transfers,
        "stats_interval": stats_interval,
    }
    changes: list[str] = []
    for key in OVERRIDABLE_KEYS:
        raw = form_map.get(key)
        if raw is None:
            continue  # field not submitted → keep current
        value = _validated(key, raw)
        if key in ("username", "password") and not value:
            continue  # blank username/password keeps the current value
        await store_setting(db, key, value)
        changes.append(f"{key}={'<set>' if key in ('password',) else value or '(env default)'}")
    await db.commit()
    await load_db_overrides()
    reschedule_self_backup()
    if changes:
        await audit.log(action="settings_updated", details=", ".join(changes))
    return RedirectResponse("/settings?saved=1", status_code=303)


class SettingsPayload(BaseModel):
    username: str | None = None
    password: str | None = None
    self_backup_dest: str | None = None
    # ints are also accepted as strings so an empty string can revert to the
    # environment default (see _validated)
    retain_days: int | str | None = None
    log_retain_days: int | str | None = None
    rclone_transfers: int | str | None = None
    stats_interval: int | str | None = None


@router.get("/api/settings")
async def api_settings_get(db=Depends(get_db)):
    view = await _settings_view(db)
    return {
        field["key"]: {
            "value": field["value"] if field["key"] != "password" else None,
            "default": field["default"],
            "source": "database" if field["overridden"] else "environment",
        }
        for field in view["fields"]
    }


@router.put("/api/settings")
async def api_settings_put(
    payload: SettingsPayload,
    request: Request,
    db=Depends(get_db),
    audit: AuditContext = Depends(audit_context),
):
    data = payload.model_dump(exclude_none=True)
    if not data:
        raise HTTPException(status_code=422, detail="no settings provided")
    changes: list[str] = []
    for key, raw in data.items():
        value = _validated(key, str(raw))
        if key in ("username", "password") and not value:
            continue  # blank keeps current
        await store_setting(db, key, value)
        changes.append(key)
    await db.commit()
    await load_db_overrides()
    reschedule_self_backup()
    if changes:
        await audit.log(action="settings_updated", details=", ".join(changes))
    view = await _settings_view(db)
    return {
        field["key"]: {
            "value": field["value"] if field["key"] != "password" else None,
            "default": field["default"],
            "source": "database" if field["overridden"] else "environment",
        }
        for field in view["fields"]
    }
