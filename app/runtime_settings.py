"""Runtime settings: environment variables are the bootstrap default and a row
in the `settings` table overrides it. Applied by mutating the `settings`
singleton so every existing call site (auth, rclone spawn, scheduler jobs)
picks up the runtime value without changes.
"""

from typing import Any

from sqlalchemy import select

from app.config import settings
from app.crypto import decrypt, encrypt, is_encrypted

# Keys the UI can manage at runtime. Everything else stays env-only.
OVERRIDABLE_KEYS = (
    "username",
    "password",
    "self_backup_dest",
    "retain_days",
    "log_retain_days",
    "rclone_transfers",
    "stats_interval",
)

INT_KEYS = {
    "retain_days",
    "log_retain_days",
    "rclone_transfers",
    "stats_interval",
}


def _coerce(key: str, value: Any) -> Any:
    value = (value or "").strip()
    if value == "":
        return None
    if key in INT_KEYS:
        try:
            return int(value)
        except ValueError:
            return None
    return value


def apply_overrides(overrides: dict[str, str]) -> dict[str, bool]:
    """Apply DB values to the settings singleton. Returns changed keys."""
    changed: dict[str, bool] = {}
    for key, raw in overrides.items():
        if key not in OVERRIDABLE_KEYS:
            continue
        value = _coerce(key, raw)
        if value is None:
            continue
        if key == "password":
            value = decrypt(value) if is_encrypted(value) else value
        current = getattr(settings, key)
        if current != value:
            setattr(settings, key, value)
            changed[key] = True
    return changed


async def load_db_overrides() -> dict[str, bool]:
    from app.database import SessionLocal
    from app.models import Setting

    async with SessionLocal() as db:
        rows = (await db.execute(select(Setting))).scalars().all()
    _reset_to_env()
    return apply_overrides({row.key: row.value for row in rows})


def effective_value(key: str) -> str:
    """Current effective value as a string (for the UI)."""
    value = getattr(settings, key, None)
    if isinstance(value, bool):
        return "true" if value else "false"
    return "" if value is None else str(value)


def env_default(key: str) -> str:
    """The env/bootstrap value, before any DB override was applied.

    Stored so the UI can show "default (env)" vs "override (database)".
    """
    return _ENV_DEFAULTS.get(key, "")


_ENV_DEFAULTS: dict[str, str] = {
    "username": settings.username,
    "password": settings.password,
    "self_backup_dest": settings.self_backup_dest,
    "retain_days": str(settings.retain_days),
    "log_retain_days": str(settings.log_retain_days),
    "rclone_transfers": str(settings.rclone_transfers),
    "stats_interval": str(settings.stats_interval),
}


def _reset_to_env() -> None:
    """Put every overridable key back to its env/bootstrap value (typed).

    Empty env defaults are skipped: ``_coerce`` maps "" to None (meaning
    "unset") and writing None into a typed settings field would corrupt it
    (e.g. an int field holding None would break arithmetic downstream).
    """
    for key, raw in _ENV_DEFAULTS.items():
        value = _coerce(key, raw)
        if value is None:
            continue
        setattr(settings, key, value)


async def store_setting(db, key: str, value: str) -> None:
    from app.models import Setting

    if value == "" or value is None:
        existing = await db.get(Setting, key)
        if existing is not None:
            await db.delete(existing)
        return
    if key == "password":
        value = encrypt(value)
    row = await db.get(Setting, key)
    if row is None:
        row = Setting(key=key, value=value)
        db.add(row)
    else:
        row.value = value
