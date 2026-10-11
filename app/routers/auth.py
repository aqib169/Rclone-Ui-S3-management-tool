"""Login / logout page. Public router (no auth dependency)."""

import asyncio
import secrets

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse

from app.auth import SESSION_COOKIE, SESSION_TTL, create_session_token
from app.config import settings
from app.dependencies import get_db, templates

router = APIRouter()


def _safe_next(next_url: str) -> str:
    next_url = (next_url or "").strip()
    if next_url.startswith("/") and not next_url.startswith("//"):
        return next_url
    return "/"


@router.get("/login")
async def login_page(request: Request, next: str = ""):
    return templates.TemplateResponse(
        request,
        "login.html",
        {"error": "", "next": _safe_next(next)},
    )


@router.post("/login")
async def login_submit(
    request: Request,
    username: str = Form(""),
    password: str = Form(""),
    next: str = Form(""),
    db=Depends(get_db),
):
    user_ok = secrets.compare_digest(username or "", settings.username)
    pass_ok = secrets.compare_digest(password or "", settings.password)
    if not (user_ok and pass_ok):
        # Constant-ish cost on failure to slow down brute-force attempts.
        await asyncio.sleep(1)
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": "Invalid username or password", "next": _safe_next(next)},
            status_code=401,
        )
    response = RedirectResponse(url=_safe_next(next), status_code=303)
    response.set_cookie(
        SESSION_COOKIE,
        create_session_token(username),
        max_age=SESSION_TTL,
        httponly=True,
        samesite="lax",
    )
    return response


@router.post("/logout")
async def logout():
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE)
    return response
