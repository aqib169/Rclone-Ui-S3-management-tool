from typing import AsyncGenerator

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import authenticate
from app.database import SessionLocal
from app.dependencies import get_db
from app.models import AuditLog


class AuditContext:
    def __init__(self, db: AsyncSession, username: str, request: Request):
        self.db = db
        self.username = username
        self.request = request
        self.action = ""
        self.details = ""
        self.status_code = 0

    def _client_ip(self) -> str:
        forwarded = self.request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()
        return (self.request.client.host if self.request.client else "") or ""

    async def log(self, action: str = "", details: str = "", status_code: int = 0) -> None:
        entry = AuditLog(
            username=self.username,
            ip_address=self._client_ip(),
            method=self.request.method,
            path=self.request.url.path,
            action=action or self.action,
            details=details or self.details,
            status_code=status_code or self.status_code,
            success=(status_code or self.status_code) < 400,
        )
        self.db.add(entry)
        await self.db.commit()


async def audit_context(
    request: Request,
    db: AsyncSession = Depends(get_db),
    username: str = Depends(authenticate),
) -> AsyncGenerator[AuditContext, None]:
    yield AuditContext(db=db, username=username, request=request)


async def log_background(action: str, details: str, status_code: int = 200) -> None:
    """Audit entry for background/scheduled activity with no HTTP request."""
    async with SessionLocal() as db:
        db.add(
            AuditLog(
                username="system",
                ip_address="",
                method="SCHEDULER",
                path="",
                action=action,
                details=details,
                status_code=status_code,
                success=status_code < 400,
            )
        )
        await db.commit()
