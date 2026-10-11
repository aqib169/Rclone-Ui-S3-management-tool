"""Authentication: session cookie for the web UI + Basic auth for the JSON API.

The UI logs in via a form (/login) and gets a signed, HttpOnly session cookie.
API clients may keep using HTTP Basic auth. Both paths validate against the
current runtime credentials (env default or UI override from the settings page).
"""

import asyncio
import base64
import hashlib
import hmac
import os
import secrets
import time
from urllib.parse import quote

from fastapi import HTTPException, Request, status
from fastapi.responses import JSONResponse, RedirectResponse

from app.config import settings

SESSION_COOKIE = "orchestrator_session"
SESSION_TTL = 7 * 24 * 3600


class AuthRequired(Exception):
    """Raised when a page requires login. Converted to a redirect (or 401 for
    API/JSON) by the exception handler in main.py. FastAPI ignores Response
    objects returned from router-level dependencies, so we signal via an
    exception instead."""

    def __init__(self, request: Request):
        self.request = request


def _session_secret() -> bytes:
    """Persistent HMAC secret for signing session cookies (stable across restarts)."""
    path = settings.data_dir / ".session_secret"
    if path.exists():
        return path.read_bytes()
    secret = secrets.token_bytes(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(secret)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return secret


def create_session_token(username: str) -> str:
    exp = int(time.time()) + SESSION_TTL
    payload = base64.urlsafe_b64encode(f"{username}.{exp}".encode()).decode().rstrip("=")
    sig = hmac.new(_session_secret(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def validate_session_token(token: str | None) -> str | None:
    if not token:
        return None
    try:
        payload, sig = token.rsplit(".", 1)
        expected = hmac.new(_session_secret(), payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return None
        decoded = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)).decode()
        username, exp = decoded.rsplit(".", 1)
        if int(exp) < time.time():
            return None
        return username
    except Exception:  # noqa: BLE001
        return None


async def _check_basic(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("basic "):
        return None
    try:
        decoded = base64.b64decode(header[6:].strip()).decode()
        username, _, password = decoded.partition(":")
    except Exception:  # noqa: BLE001
        return None
    user_ok = secrets.compare_digest(username, settings.username)
    pass_ok = secrets.compare_digest(password, settings.password)
    if user_ok and pass_ok:
        return username
    # Constant-ish cost on failure to slow down brute-force attempts.
    await asyncio.sleep(1)
    return None


def login_redirect(request: Request):
    """Response for an unauthenticated request: redirect browsers to /login,
    hand htmx a redirect via header, or return 401 JSON for API clients."""
    next_url = request.url.path
    if request.url.query:
        next_url += "?" + request.url.query
    login_url = f"/login?next={quote(next_url)}"
    if request.headers.get("hx-request") == "true":
        # htmx: hand the navigation to the login page via response header.
        return JSONResponse(
            status_code=401,
            content={"detail": "Not authenticated"},
            headers={"HX-Redirect": login_url},
        )
    accept = request.headers.get("accept", "")
    if "text/html" in accept:
        return RedirectResponse(url=login_url, status_code=status.HTTP_303_SEE_OTHER)
    return JSONResponse(
        status_code=status.HTTP_401_UNAUTHORIZED,
        content={"detail": "Not authenticated"},
        headers={"WWW-Authenticate": "Basic"},
    )


async def authenticate(request: Request) -> str:
    username = validate_session_token(request.cookies.get(SESSION_COOKIE))
    if username is None:
        username = await _check_basic(request)
    if username is not None:
        return username

    if request.url.path.startswith("/api"):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Basic"},
        )
    raise AuthRequired(request)
