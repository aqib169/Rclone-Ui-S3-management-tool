import re
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field, field_validator

REMOTE_NAME_PATTERN = r"^[a-z0-9_-]+$"

# host[:port] with an optional IPv6 bracket form — what a self-hosted endpoint
# looks like when the scheme is left out.
_HOST_PORT_RE = re.compile(
    r"^(?:\[[0-9a-fA-F:]+\]|[A-Za-z0-9._-]+)(?::\d{1,5})?$"
)

_CRON_RANGES = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 6)]


def _cron_field_valid(field: str, lo: int, hi: int) -> bool:
    for part in field.split(","):
        part = part.strip()
        step = 1
        base = part
        if "/" in part:
            base, _, step_s = part.partition("/")
            if not step_s.isdigit() or int(step_s) < 1:
                return False
            step = int(step_s)
        if base == "*":
            continue
        if "-" in base:
            a, _, b = base.partition("-")
            if not (a.isdigit() and b.isdigit()):
                return False
            a, b = int(a), int(b)
            if not (lo <= a <= hi and lo <= b <= hi and a <= b):
                return False
            continue
        if not base.isdigit():
            return False
        if not (lo <= int(base) <= hi):
            return False
        _ = step
    return True


def sanitize_cron_schedule(value: str) -> str:
    """Auto-sanitize a cron expression before it is stored/scheduled.

    Safety rule: when the minute field is ``*`` alongside a step-hour schedule
    (``*/X``), the expression would fire every minute during those hours.
    Rewrite the minute to ``0`` so ``* */3 * * *`` becomes ``0 */3 * * *``.
    Invalid / non-step expressions are returned unchanged.
    """
    value = (value or "").strip()
    if not value:
        return value
    parts = value.split()
    if len(parts) != 5:
        return value
    if parts[0] == "*" and parts[1].startswith("*/"):
        parts[0] = "0"
        return " ".join(parts)
    return value


def validate_cron_schedule(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return value
    parts = value.split()
    if len(parts) != 5:
        raise ValueError("schedule_cron must be a 5-field cron expression (minute hour day month weekday)")
    names = ["minute", "hour", "day-of-month", "month", "weekday"]
    for field, (lo, hi), name in zip(parts, _CRON_RANGES, names):
        if not _cron_field_valid(field, lo, hi):
            raise ValueError(f"invalid {name} value '{field}' (expected range {lo}-{hi}, *, */n, n-m)")
    return sanitize_cron_schedule(value)


def _valid_endpoint(v: str) -> str:
    """Accept what people actually type for an S3 endpoint.

    A self-hosted server is usually entered as `192.168.1.10:3900` or
    `garage.lan:3900`, without a scheme — so a bare `host:port` is accepted and
    normalised to `http://…`. Full URLs and absolute paths (`local` remotes)
    are passed through unchanged.
    """
    v = (v or "").strip().rstrip("/")
    if not v:
        raise ValueError("endpoint is required")
    if v.startswith("/"):
        # local remote: endpoint is a host path inside the container
        return v
    if not (v.startswith("http://") or v.startswith("https://")):
        # Bare host, host:port or a hostname — default to plain http.
        if any(ch.isspace() for ch in v):
            raise ValueError("endpoint must not contain spaces")
        if _HOST_PORT_RE.match(v):
            return f"http://{v}"
        raise ValueError(
            "endpoint must be a URL (https://…), a host with an optional port "
            "(192.168.1.10:3900), or an absolute path for local remotes"
        )
    rest = v.split("://", 1)[1]
    if not rest:
        raise ValueError("endpoint must include a host")
    return v


class RemoteCreate(BaseModel):
    name: str = Field(min_length=1, max_length=64, pattern=REMOTE_NAME_PATTERN)
    endpoint: str = Field(min_length=1, max_length=256)
    # Credentials also arrive through `options` (access_key_id /
    # secret_access_key for s3, account/key for b2, …), which is what the form
    # sends; these legacy columns stay for API compatibility and are optional.
    # 4096 matches the option path: a wrongly pasted value must reach the
    # diagnostics that explain it, not be rejected as a 422 before the user
    # learns what was wrong with it.
    access_key: str = Field(default="", max_length=4096)
    secret_key: str = Field(default="", max_length=4096)
    region: str = Field(default="garage", max_length=64)
    type: str = Field(default="s3", max_length=64)
    provider: str = Field(default="Other", max_length=32)
    # Optional bucket for bucket-based backends. Essential for a bucket-scoped
    # Cloudflare R2 token, which cannot list buckets and is therefore unusable
    # without being told which bucket to use.
    bucket: str = Field(default="", max_length=128)
    # Arbitrary rclone config options for any backend. Values of secret options
    # are encrypted at rest (see app.remote_config.normalize_options).
    options: dict = Field(default_factory=dict)

    @field_validator("endpoint")
    @classmethod
    def _check_endpoint(cls, v: str) -> str:
        return _valid_endpoint(v)


class RemoteUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=64, pattern=REMOTE_NAME_PATTERN)
    endpoint: Optional[str] = Field(default=None, min_length=1, max_length=256)
    access_key: Optional[str] = Field(default=None, max_length=4096)
    secret_key: Optional[str] = Field(default=None, max_length=4096)
    region: Optional[str] = Field(default=None, max_length=64)
    type: Optional[str] = Field(default=None, max_length=64)
    provider: Optional[str] = Field(default=None, max_length=32)
    # Default bucket; send "" to clear it.
    bucket: Optional[str] = Field(default=None, max_length=128)
    # Replaces the stored option map wholesale when provided. Secret values
    # that are left out are removed, so an edit never leaves a stale secret.
    options: Optional[dict] = None

    @field_validator("endpoint")
    @classmethod
    def _check_endpoint(cls, v: Optional[str]) -> Optional[str]:
        return _valid_endpoint(v) if v else v


class RemoteOut(BaseModel):
    id: int
    name: str
    type: str
    provider: str
    endpoint: str
    region: str
    bucket: str = ""
    created_at: datetime
    updated_at: datetime | None = None

    model_config = {"from_attributes": True}


class RemoteTestResult(BaseModel):
    ok: bool
    error: Optional[str] = None
    # Non-fatal advice, e.g. a bucket-scoped R2 token that cannot list buckets.
    warning: Optional[str] = None
    buckets: list[str] = []
    # What the server actually received for the credentials, masked. A server
    # saying "140 characters" while the field looks like 32 dots is otherwise
    # impossible to reason about — this makes it checkable.
    received: Optional[str] = None


class JobCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    source_remote: str = Field(min_length=1, max_length=64)
    source_bucket: str = Field(default="", max_length=500)
    source_path: str = Field(default="", max_length=500)
    dest_remote: str = Field(min_length=1, max_length=64)
    dest_bucket: str = Field(default="", max_length=500)
    dest_path: str = Field(default="", max_length=500)
    schedule_cron: str = Field(default="", max_length=100)
    enabled: bool = True

    @field_validator("schedule_cron")
    @classmethod
    def _valid_cron(cls, v: str) -> str:
        return validate_cron_schedule(v)


class JobUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=200)
    source_remote: Optional[str] = Field(default=None, max_length=64)
    source_bucket: Optional[str] = Field(default=None, max_length=500)
    source_path: Optional[str] = Field(default=None, max_length=500)
    dest_remote: Optional[str] = Field(default=None, max_length=64)
    dest_bucket: Optional[str] = Field(default=None, max_length=500)
    dest_path: Optional[str] = Field(default=None, max_length=500)
    schedule_cron: Optional[str] = Field(default=None, max_length=100)
    enabled: Optional[bool] = None

    @field_validator("schedule_cron")
    @classmethod
    def _valid_cron(cls, v: Optional[str]) -> Optional[str]:
        return validate_cron_schedule(v) if v else v


class JobOut(BaseModel):
    id: int
    name: str
    source_remote: str
    source_bucket: str
    source_path: str
    dest_remote: str
    dest_bucket: str
    dest_path: str
    schedule: str
    enabled: bool
    created_at: datetime
    updated_at: datetime
    last_run_status: Optional[str] = None
    last_run_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class RunOut(BaseModel):
    run_id: str
    job_id: Optional[int]
    kind: str
    source: str
    destination: str
    status: str
    triggered_by: str
    started_at: Optional[datetime]
    finished_at: Optional[datetime]
    exit_code: Optional[int]
    files: int
    size_bytes: int
    error: str

    model_config = {"from_attributes": True}


class RestoreRequest(BaseModel):
    source: str = Field(min_length=1, max_length=500)
    destination: str = Field(min_length=1, max_length=500)


class RunRequest(BaseModel):
    source: str = Field(min_length=1, max_length=500)
    destination: str = Field(min_length=1, max_length=500)
    kind: str = Field(default="restore", pattern="^(backup|restore)$")