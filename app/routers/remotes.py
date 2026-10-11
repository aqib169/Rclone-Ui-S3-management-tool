import json
import re

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import AuditContext, audit_context
from app.auth import authenticate
from app.crypto import encrypt
from app.dependencies import get_db
from app.models import Job, Remote
from app.rclone import list_buckets, test_connection, test_connection_inline
from app.remote_config import (
    OptionError,
    allowed_option_names,
    decrypt_options,
    normalize_options,
    options_summary,
)
from app.schemas import (
    REMOTE_NAME_PATTERN,
    RemoteCreate,
    RemoteOut,
    RemoteTestResult,
    RemoteUpdate,
)

router = APIRouter(prefix="/api", tags=["remotes"], dependencies=[Depends(authenticate)])

_remote_name_re = re.compile(REMOTE_NAME_PATTERN)


def _bad_name() -> HTTPException:
    return HTTPException(status_code=400, detail=f"name must match {REMOTE_NAME_PATTERN}")


def _remote_dict(remote: Remote) -> dict:
    """Remote + its non-secret option names (values are never returned)."""
    data = RemoteOut.model_validate(remote).model_dump()
    data["options"] = options_summary(remote)
    return data


@router.get("/remotes")
async def list_remotes(db: AsyncSession = Depends(get_db)):
    remotes = (
        await db.execute(select(Remote).order_by(Remote.name))
    ).scalars().all()
    return {"remotes": [_remote_dict(r) for r in remotes]}


@router.get("/remotes/{name}/buckets")
async def remote_buckets(name: str, db: AsyncSession = Depends(get_db)):
    remote = (
        await db.execute(select(Remote).where(Remote.name == name))
    ).scalar_one_or_none()
    if remote is None:
        raise HTTPException(status_code=400, detail=f"remote '{name}' does not exist")
    try:
        buckets = await list_buckets(name)
    except Exception as exc:  # noqa: BLE001
        # A bucket-scoped token cannot list buckets, but the remote's own
        # bucket is known, so offer that rather than an empty list.
        saved = (getattr(remote, "bucket", "") or "").strip()
        if saved:
            return {"buckets": [saved], "scoped": True}
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"buckets": buckets}


@router.post("/remotes", status_code=201)
async def create_remote(
    payload: RemoteCreate,
    db: AsyncSession = Depends(get_db),
    audit: AuditContext = Depends(audit_context),
):
    if not _remote_name_re.match(payload.name):
        raise _bad_name()
    exists = (
        await db.execute(select(Remote).where(Remote.name == payload.name))
    ).scalar_one_or_none()
    if exists is not None:
        raise HTTPException(status_code=409, detail="remote already exists")

    backend = (payload.type or "s3").strip()
    try:
        options = normalize_options(
            payload.options or {}, backend, allowed_option_names(backend)
        )
    except OptionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # Credentials usually arrive inside `options` (the generated form sends
    # access_key_id / secret_access_key); the legacy columns are a fallback and
    # are filled from the option map so old queries keep working.
    access_key = payload.access_key or str(options.get("access_key_id") or "")
    secret_key = payload.secret_key or str(options.get("secret_access_key") or "")
    region = payload.region or str(options.get("region") or "garage")
    provider = payload.provider or str(options.get("provider") or "Other")
    bucket = (payload.bucket or "").strip()

    remote = Remote(
        name=payload.name,
        type=backend,
        provider=provider,
        endpoint=payload.endpoint,
        region=region,
        bucket=bucket,
        access_key=encrypt(access_key),
        secret_key=encrypt(secret_key),
        options=json.dumps(options),
    )
    db.add(remote)
    await db.commit()
    await db.refresh(remote)
    await audit.log(
        action="remote_created",
        details=f"remote={remote.name} type={remote.type}",
    )
    return _remote_dict(remote)


@router.put("/remotes/{remote_id}")
async def update_remote(
    remote_id: int,
    payload: RemoteUpdate,
    db: AsyncSession = Depends(get_db),
    audit: AuditContext = Depends(audit_context),
):
    remote = await db.get(Remote, remote_id)
    if remote is None:
        raise HTTPException(status_code=404, detail="remote not found")

    data = payload.model_dump(exclude_unset=True)
    if "name" in data and data["name"] is not None:
        if not _remote_name_re.match(data["name"]):
            raise _bad_name()
        dup = (
            await db.execute(
                select(Remote).where(Remote.name == data["name"], Remote.id != remote_id)
            )
        ).scalar_one_or_none()
        if dup is not None:
            raise HTTPException(status_code=409, detail="remote already exists")
        remote.name = data["name"]
    if data.get("type"):
        remote.type = data["type"].strip()
    for field in ("provider", "endpoint", "region"):
        if data.get(field):
            setattr(remote, field, data[field])
    # The bucket is explicitly clearable, unlike the other legacy columns.
    if data.get("bucket") is not None:
        remote.bucket = str(data["bucket"]).strip()
    # blank credentials keep the existing ones
    if data.get("access_key"):
        remote.access_key = encrypt(data["access_key"])
    if data.get("secret_key"):
        remote.secret_key = encrypt(data["secret_key"])
    if data.get("options") is not None:
        backend = (remote.type or "s3").strip()
        try:
            options = normalize_options(
                data["options"], backend, allowed_option_names(backend)
            )
        except OptionError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        remote.options = json.dumps(options)

    await db.commit()
    await audit.log(action="remote_updated", details=f"remote={remote.name} id={remote_id}")
    return _remote_dict(remote)


@router.delete("/remotes/{remote_id}", status_code=204)
async def delete_remote(
    remote_id: int,
    db: AsyncSession = Depends(get_db),
    audit: AuditContext = Depends(audit_context),
):
    remote = await db.get(Remote, remote_id)
    if remote is None:
        raise HTTPException(status_code=404, detail="remote not found")
    in_use = (
        await db.execute(
            select(Job).where(
                or_(Job.source_remote == remote.name, Job.dest_remote == remote.name)
            )
        )
    ).scalars().first()
    if in_use is not None:
        raise HTTPException(
            status_code=409,
            detail=f"remote '{remote.name}' is used by job '{in_use.name}'",
        )
    name = remote.name
    await db.delete(remote)
    await db.commit()
    await audit.log(action="remote_deleted", details=f"remote={name} id={remote_id}")


@router.post("/remotes/{remote_id}/test", response_model=RemoteTestResult)
async def test_remote(
    remote_id: int,
    db: AsyncSession = Depends(get_db),
):
    remote = await db.get(Remote, remote_id)
    if remote is None:
        raise HTTPException(status_code=404, detail="remote not found")
    # Use the remote's own bucket: with a bucket-scoped token that is the only
    # root that can be listed at all.
    ok, error, buckets, warning = await test_connection(
        remote.name, getattr(remote, "bucket", "") or ""
    )
    return RemoteTestResult(
        ok=ok, error=error or None, warning=warning or None, buckets=buckets
    )


def _describe_received(payload: "RemoteCreate", options: dict) -> str:
    """Masked summary of the credentials the server actually received.

    Shown next to the result so a length complaint from the provider can be
    checked against what was sent, instead of the user having to trust that a
    32-character-looking field did not arrive as something else (a password
    manager's autofill, a stray paste, an untrimmed newline).
    """
    parts: list[str] = []
    key = str(options.get("access_key_id") or payload.access_key or "")
    if key:
        parts.append(
            f"access key id: {len(key)} chars, starting "
            f"'{key[:6]}'{'…' if len(key) > 6 else ''}"
        )
    secret = str(options.get("secret_access_key") or payload.secret_key or "")
    if secret:
        parts.append(f"secret key: {len(secret)} chars")
    endpoint = str(options.get("endpoint") or payload.endpoint or "")
    if endpoint:
        parts.append(f"endpoint: {endpoint}")
    return " | ".join(parts)


@router.post("/remotes/test", response_model=RemoteTestResult)
async def test_remote_inline(payload: RemoteCreate):
    """Test an unsaved remote (the Add Remote modal's "Test first" button).

    Works for any backend: the legacy S3 fields are merged with the submitted
    `options`, so a B2/SFTP/GCS remote can be validated before it is saved.
    """
    if not _remote_name_re.match(payload.name):
        raise _bad_name()
    backend = (payload.type or "s3").strip()
    try:
        stored = normalize_options(
            payload.options or {}, backend, allowed_option_names(backend)
        )
    except OptionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # normalize_options ENCRYPTS secret options for storage at rest. rclone must
    # never see that: a 32-character key becomes a 140-character Fernet token,
    # which is what the provider was really complaining about all along ("access
    # key has length 140, should be 32"). Test with the plaintext.
    options = decrypt_options(stored, backend)
    ok, error, buckets, warning = await test_connection_inline(
        payload.name,
        payload.endpoint,
        payload.access_key,
        payload.secret_key,
        payload.region,
        backend,
        payload.provider,
        options,
        payload.bucket,
    )
    return RemoteTestResult(
        ok=ok,
        error=error or None,
        warning=warning or None,
        buckets=buckets,
        received=_describe_received(payload, options),
    )