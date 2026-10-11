from typing import AsyncGenerator

import re

from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import APP_VERSION, settings
from app.database import SessionLocal


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with SessionLocal() as session:
        yield session


templates = Jinja2Templates(directory=str(settings.templates_dir))


def _asset_version() -> str:
    """Cache-busting token for /static assets: the app version plus the
    modification time of each vendored file, so browsers always fetch a fresh
    copy after an upgrade or rebuild (no stale-CSS confusion)."""
    token = APP_VERSION
    for name in ("tailwind.css", "htmx.min.js"):
        try:
            token += f"-{int((settings.static_dir / 'vendor' / name).stat().st_mtime)}"
        except OSError:
            continue
    return token


templates.env.globals["asset_version"] = _asset_version()
templates.env.globals["app_version"] = APP_VERSION


def _dt(value):
    if value is None:
        return "-"
    return value.strftime("%Y-%m-%d %H:%M:%S")


def _bytes(value):
    if not value:
        return "0 B"
    size = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} TiB"


templates.env.filters["dt"] = _dt
templates.env.filters["bytes"] = _bytes


def _target(remote: str, bucket: str = "", path: str = "") -> str:
    bucket = (bucket or "").strip("/")
    path = (path or "").strip("/")
    parts = [p for p in (bucket, path) if p]
    return f"{remote}:{'/'.join(parts)}"


def _slug(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]+", "-", value or "").strip("-")


templates.env.filters["target"] = _target
templates.env.filters["slug"] = _slug
