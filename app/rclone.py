import asyncio
import json
import os
import re
import signal
import tempfile
from pathlib import Path

from sqlalchemy import select

from app.config import settings
from app.database import SessionLocal
from app.models import Remote
from app import diagnostics
from app.remote_config import encode_config, resolve_options

# Hard ceiling on any single rclone invocation. Interactive calls (Test First,
# listing) must fail with a message rather than hang; long transfers pass their
# own, much larger, budget.
RCLONE_TIMEOUT = 45.0


def _build_target(remote: str, path: str) -> str:
    path = path.strip("/")
    return f"{remote}:{path}"


async def _remote_row(name: str):
    async with SessionLocal() as db:
        return (
            await db.execute(select(Remote).where(Remote.name == name))
        ).scalar_one_or_none()


async def _resolve_target(remote: str, path: str) -> str:
    """Build the rclone target for a remote, inlining the endpoint path for
    `local` remotes (rclone's local backend roots at the process CWD, so the
    path must be part of the remote reference).

    For a bucket-based remote the saved `bucket` is used when no path was
    given. That is what makes a bucket-scoped Cloudflare R2 token usable at
    all: it cannot list buckets, so `remote:` alone has nothing to list and
    the remote would look permanently empty.
    """
    if not remote:
        return _build_target(remote, path)
    row = await _remote_row(remote)
    if row is not None and row.type == "local":
        root = (row.endpoint or "").rstrip("/")
        rest = path.strip("/")
        combined = f"{root}/{rest}" if rest else root
        return f"{remote}:{combined}"
    if row is not None and not path.strip("/"):
        bucket = (getattr(row, "bucket", "") or "").strip("/")
        if bucket:
            return f"{remote}:{bucket}"
    return _build_target(remote, path)


async def _expand_target(target: str) -> str:
    """Expand a `remote:path` transfer target for local remotes. Plain paths
    (e.g. the self-backup snapshot dir) and non-local remotes are unchanged."""
    m = re.match(r"^([^:]+):(.*)$", target)
    if not m:
        return target
    name, rest = m.group(1), m.group(2)
    row = await _remote_row(name)
    if row is not None and row.type == "local":
        root = (row.endpoint or "").rstrip("/")
        rest = rest.strip("/")
        return f"{name}:{root + '/' + rest if rest else root}"
    return target


async def write_temp_config() -> str:
    """Write a temp rclone.conf covering every configured remote.

    Each remote is rendered from its resolved option map by
    ``app.remote_config.encode_config``, so every rclone backend the app knows
    about (s3, local, b2, google cloud storage, sftp, …) is supported, not just
    S3. Returns the config path; the caller must unlink it.
    """
    async with SessionLocal() as db:
        remotes = (
            await db.execute(select(Remote).order_by(Remote.name))
        ).scalars().all()
        sections = [
            encode_config(r.name, (r.type or "s3").strip(), resolve_options(r))
            for r in remotes
        ]

    fd, path = tempfile.mkstemp(prefix="rclone-", suffix=".conf", dir="/tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write("\n".join(sections))
        os.chmod(path, 0o600)
    except Exception:
        try:
            os.unlink(path)
        except OSError:
            pass
        raise
    return path


def _write_single_temp_config(
    name: str,
    endpoint: str = "",
    access_key: str = "",
    secret_key: str = "",
    region: str = "garage",
    type_: str = "s3",
    provider: str = "Other",
    options: dict | None = None,
) -> str:
    """Temp config for a single, possibly-unsaved remote.

    Accepts either the legacy S3 field set or an arbitrary option map so any
    rclone backend can be tested before it is saved.
    """
    resolved = dict(options or {})
    if type_ == "s3":
        resolved.setdefault("provider", provider)
        if endpoint:
            resolved.setdefault("endpoint", endpoint)
        if access_key:
            resolved.setdefault("access_key_id", access_key)
        if secret_key:
            resolved.setdefault("secret_access_key", secret_key)
        if region:
            resolved.setdefault("region", region)
    elif endpoint and type_ == "local":
        # rclone's local backend has no root option; the path is inlined into
        # the target by _resolve_target/_expand_target.
        pass
    elif endpoint:
        resolved.setdefault("endpoint", endpoint)

    fd, path = tempfile.mkstemp(prefix="rclone-", suffix=".conf", dir="/tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(encode_config(name, type_, resolved))
        os.chmod(path, 0o600)
    except Exception:
        try:
            os.unlink(path)
        except OSError:
            pass
        raise
    return path


async def _run_rclone(
    config_path: str, args: list[str], timeout: float | None = None
) -> tuple[int, str, str]:
    """Run rclone with list args (never shell), using the given temp config.

    Always bounded by a timeout. rclone's own --contimeout only covers
    connection setup: a server that accepts the connection and then stalls
    (or a slow provider) would otherwise hang the HTTP request forever, leaving
    the Test First button spinning with no message at all. Long transfers pass
    `timeout=None` so they are never cut short.
    """
    budget = RCLONE_TIMEOUT if timeout is None else timeout
    proc = await asyncio.create_subprocess_exec(
        settings.rclone_bin,
        "--config",
        config_path,
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        if budget is None:
            stdout, stderr = await proc.communicate()
        else:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=budget
            )
    except asyncio.TimeoutError:
        _kill(proc)
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except asyncio.TimeoutError:  # pragma: no cover - process is unkillable
            pass
        return (
            1,
            "",
            f"rclone did not finish within {budget:.0f}s "
            f"({' '.join(args[:3])}). The endpoint accepted the connection but did "
            f"not respond in time.",
        )
    code = proc.returncode if proc.returncode is not None else -1
    return code, stdout.decode(errors="replace"), stderr.decode(errors="replace")


def _kill(proc) -> None:
    """Terminate a stuck rclone, escalating to SIGKILL if it ignores TERM."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            if proc.returncode is not None:
                return
            proc.send_signal(sig)
        except (ProcessLookupError, OSError):
            return


def _quick_flags() -> list[str]:
    """Flags for short interactive calls: fail fast on unreachable endpoints."""
    return ["--contimeout", "5s", "--retries", "1", "--low-level-retries", "1"]


def _clean_rclone_err(err: str) -> str:
    """Reduce rclone stderr (timestamped, multi-line) to the last meaningful line."""
    if not err:
        return ""
    lines = [l for l in err.strip().splitlines() if l.strip()]
    cleaned: list[str] = []
    for line in lines:
        parts = line.split(" ", 2)
        if len(parts) == 3 and "/" in parts[0] and ":" in parts[1] and len(parts[0]) == 10:
            line = parts[2]
        if line.startswith("NOTICE:"):
            continue
        if line.startswith("ERROR :"):
            line = line[len("ERROR :"):].strip()
        cleaned.append(line)
    if not cleaned:
        return err.strip()
    return cleaned[-1]


async def _with_config(args: list[str]) -> tuple[int, str, str]:
    config_path = await write_temp_config()
    try:
        return await _run_rclone(config_path, args)
    finally:
        try:
            os.unlink(config_path)
        except OSError:
            pass


async def list_remotes() -> list[str]:
    """Remotes come from the DB, not from a host rclone.conf."""
    async with SessionLocal() as db:
        remotes = (
            await db.execute(select(Remote).order_by(Remote.name))
        ).scalars().all()
    return [r.name for r in remotes]


async def list_buckets(remote: str) -> list[str]:
    """Buckets visible on a remote.

    Deliberately bypasses the remote's saved bucket: that bucket is the root
    for transfers, but the *bucket list* must come from the remote root, or a
    remote with a bucket saved would return that bucket's contents as though
    they were buckets.
    """
    code, out, err = await _with_config(
        _quick_flags() + ["lsf", _build_target(remote, ""), "--dirs-only"]
    )
    if code != 0:
        raise RuntimeError(err.strip() or f"rclone lsf failed (exit {code})")
    seen: set = set()
    buckets: list = []
    for line in out.splitlines():
        name = line.strip().rstrip("/")
        if name and name not in seen:
            seen.add(name)
            buckets.append(name)
    return buckets


async def test_connection(
    remote: str, bucket: str = ""
) -> tuple[bool, str, list[str], str]:
    """Re-test a saved remote. Same `(ok, error, buckets, warning)` contract."""
    code, out, err = await _with_config(
        _quick_flags() + ["lsf", await _resolve_target(remote, bucket), "--max-depth", "1"]
    )
    if code != 0:
        provider = ""
        try:
            from app.database import SessionLocal
            from app.models import Remote
            from sqlalchemy import select

            async with SessionLocal() as db:
                row = (
                    await db.execute(select(Remote).where(Remote.name == remote))
                ).scalar_one_or_none()
                provider = (row.provider if row else "") or ""
        except Exception:  # noqa: BLE001 - diagnostics must never break the test
            provider = ""
        if diagnostics.can_list_buckets(err):
            return True, "", [], diagnostics.list_bucket_notice()
        return False, diagnostics.explain(_clean_rclone_err(err), provider), [], ""
    buckets = [line.strip().rstrip("/") for line in out.splitlines() if line.strip()]
    return True, "", buckets, ""


async def test_connection_inline(
    name: str,
    endpoint: str = "",
    access_key: str = "",
    secret_key: str = "",
    region: str = "garage",
    type_: str = "s3",
    provider: str = "Other",
    options: dict | None = None,
    bucket: str = "",
) -> tuple[bool, str, list[str], str]:
    """Probe a remote before saving it.

    Returns `(ok, error, buckets, warning)`. `ok` means rclone reached the
    endpoint and the credentials worked; `warning` carries non-fatal advice —
    most importantly that a bucket-scoped Cloudflare R2 token cannot list
    buckets, which must not make an otherwise usable remote look broken.

    `bucket` is optional. When given it is verified, which is how a
    bucket-scoped token is proven good: listing buckets is denied, so the
    bucket itself is the only thing that can be tested.
    """
    options = dict(options or {})
    bucket = (bucket or "").strip()
    # The API accepts both shapes (legacy access_key/secret_key/endpoint columns
    # and the `options` map), so diagnose against the same merged view the
    # generated rclone config uses.
    effective = dict(options)
    for key, value in (
        ("access_key_id", access_key),
        ("secret_access_key", secret_key),
        ("endpoint", endpoint),
        ("region", region),
        ("provider", provider),
    ):
        if value:
            effective.setdefault(key, value)
    hint = diagnostics.preflight(type_, effective)
    if hint:
        return False, hint, [], ""

    config_path = _write_single_temp_config(
        name, endpoint, access_key, secret_key, region, type_, provider, options
    )
    try:
        if type_ == "local":
            target = f"{name}:{(endpoint or '').rstrip('/')}"
        else:
            root = _root_target_for(name, type_, options)
            target = f"{name}:{root}" if root else f"{name}:"
        code, out, err = await _run_rclone(
            config_path,
            _quick_flags() + ["lsf", target, "--max-depth", "1"],
        )
        if code != 0:
            if diagnostics.can_list_buckets(err):
                # Credentials were accepted; only the bucket listing was denied.
                # A bucket-scoped token is the normal case for Cloudflare R2, so
                # this remote is usable — and if a bucket was named, prove it
                # really works rather than leaving the user to find out later.
                if bucket and type_ == "s3":
                    return await _verify_named_bucket(
                        config_path, name, bucket, provider
                    )
                return True, "", [], diagnostics.list_bucket_notice()
            return False, diagnostics.explain(_clean_rclone_err(err), provider), [], ""
        buckets = [
            line.strip().rstrip("/") for line in out.splitlines() if line.strip()
        ]
        if bucket and type_ == "s3":
            # The bucket was named: check it is real and reachable. A wrong
            # bucket name is otherwise invisible until a backup silently fails.
            if bucket not in buckets:
                known = ", ".join(buckets[:8]) or "none visible"
                return False, (
                    f"Bucket '{bucket}' was not found on this remote "
                    f"(visible buckets: {known}). Check the name, or clear the "
                    f"bucket field to use the whole remote."
                ), buckets, ""
            return True, "", buckets, f"Bucket '{bucket}' is reachable."
        return True, "", buckets, ""
    finally:
        try:
            os.unlink(config_path)
        except OSError:
            pass


async def _verify_named_bucket(
    config_path: str, name: str, bucket: str, provider: str
) -> tuple[bool, str, list[str], str]:
    """Confirm a named bucket works when ListBuckets is denied.

    This is the case a bucket-scoped Cloudflare R2 token produces: listing
    buckets is forbidden, but objects in its own bucket are fully accessible.
    Listing the bucket directly proves the credentials and the bucket name are
    both right, which is exactly what the user cannot otherwise see.
    """
    code, out, err = await _run_rclone(
        config_path,
        _quick_flags() + ["lsf", f"{name}:{bucket}", "--max-depth", "1"],
    )
    if code != 0:
        return (
            False,
            f"The credentials worked, but bucket '{bucket}' could not be read "
            f"(it may not exist, or this token may not have access to it): "
            + diagnostics.explain(_clean_rclone_err(err), provider),
            [],
            "",
        )
    entries = [line.strip() for line in out.splitlines() if line.strip()]
    return (
        True,
        "",
        [],
        diagnostics.bucket_scoped_notice(bucket, entries),
    )


def _root_target_for(name: str, type_: str, options: dict) -> str:
    """Root path to list for a backend, e.g. `/` for sftp or `` for b2."""
    if type_ in ("s3", "b2", "azureblob", "internetarchive"):
        return ""
    root = (options.get("root") or "").strip("/")
    return f"{root}/" if root else ""


async def remote_about(remote: str, path: str = "") -> dict | None:
    """Quota information (`rclone about`) when the backend supports it."""
    config_path = await write_temp_config()
    try:
        code, out, _err = await _run_rclone(
            config_path,
            # --json is required: without it rclone prints a human table.
            _quick_flags() + ["about", await _resolve_target(remote, path), "--json"],
        )
        if code != 0:
            return None
        data = json.loads(out)
    except (json.JSONDecodeError, OSError):
        return None
    finally:
        try:
            os.unlink(config_path)
        except OSError:
            pass
    total = data.get("total") or 0
    used = data.get("used") or 0
    return {
        "total": int(total),
        "used": int(used),
        "free": int(data.get("free") or 0),
        "trashed": int(data.get("trashed") or 0),
        "other": int(data.get("other") or 0),
        "used_pct": round(100 * used / total, 1) if total else 0.0,
    }


async def list_entries(
    remote: str, path: str = "", dirs_only: bool = False, max_depth: int = 1
) -> list[dict]:
    """List one directory of any backend (used by the path browser).

    Returns `[{name, path, is_dir, size, mime}]`. Never raises for a missing
    path: an empty list plus the rclone error is returned by the callers that
    need to distinguish the two.
    """
    args = _quick_flags() + [
        "lsjson",
        await _resolve_target(remote, path),
        "--max-depth",
        str(max_depth),
    ]
    if dirs_only:
        args.append("--dirs-only")
    code, out, err = await _with_config(args)
    if code != 0:
        raise RuntimeError(_clean_rclone_err(err) or f"rclone lsjson failed (exit {code})")
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return []
    return [
        {
            "name": item.get("Name", ""),
            "path": item.get("Path", ""),
            "is_dir": bool(item.get("IsDir")),
            "size": item.get("Size", 0),
            "mime": item.get("MimeType", ""),
            "mod_time": item.get("ModTime", ""),
        }
        for item in data
    ]


async def list_archive_tree(remote: str, path: str = "") -> list[dict]:
    code, out, err = await _with_config(
        _quick_flags() + ["lsjson", await _resolve_target(remote, path)]
    )
    if code != 0:
        raise RuntimeError(err.strip() or f"rclone lsjson failed (exit {code})")
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return []
    return [
        {
            "name": item.get("Name", ""),
            "path": item.get("Path", ""),
            "is_dir": bool(item.get("IsDir")),
            "size": item.get("Size", 0),
            "mime": item.get("MimeType", ""),
        }
        for item in data
    ]


# Only timestamped delta folders under /archive/ are recognized. The /latest/
# baseline mirror lives outside /archive/ and is never listed here, so
# retention can never prune it.
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}$")


async def archive_dates(remote: str, bucket: str) -> list[str]:
    """List YYYY-MM-DD[_HH-MM-SS] archive folders under remote:bucket/archive/,
    newest first."""
    code, out, err = await _with_config(
        _quick_flags()
        + ["lsf", await _resolve_target(remote, f"{bucket}/archive"), "--max-depth", "1"]
    )
    if code != 0:
        return []
    dates = [
        line.strip().rstrip("/") for line in out.splitlines() if line.strip()
    ]
    return sorted((d for d in dates if _DATE_RE.match(d)), reverse=True)


async def purge_archive_date(remote: str, bucket: str, date: str) -> int:
    """Remove an archive date folder entirely."""
    code, _out, _err = await _with_config(
        ["purge", await _resolve_target(remote, f"{bucket}/archive/{date}")]
    )
    return code if code is not None else -1


# Directory-valued flags whose value must be expanded for `local` remotes
# (rclone's local backend has no root option, so the endpoint path is inlined).
_DIR_VALUE_FLAGS = {"--backup-dir", "--backup-dir1", "--backup-dir2", "--compare-dest"}


async def global_flags(
    config_path: str,
    log_path: Path,
    stats_interval: int | None = None,
    checksums: bool | None = None,
    transfers: int | None = None,
) -> list[str]:
    """Global flags shared by every operation (config, logging, stats).

    `--use-json-log` is what makes structured progress possible (see
    `app/progress.py`). Everything here is a fixed flag set; per-run overrides
    come from the operation registry in `app/operations.py`.
    """
    args = [
        "--config",
        config_path,
        "--log-file",
        str(log_path),
        "--log-level",
        "INFO",
        "--use-json-log",
        "--stats",
        f"{stats_interval or settings.stats_interval}s",
        "--stats-log-level",
        "INFO",
        "--stats-one-line",
        "--transfers",
        str(transfers or settings.rclone_transfers),
        "--checkers",
        "8",
        "--contimeout",
        "5s",
        "--retries",
        "1",
        "--low-level-retries",
        "1",
        "--no-update-modtime",
    ]
    # Checksum comparison is on by default (as in v2.0) so a same-size modified
    # file is still re-copied; a run can opt out explicitly.
    if settings.rclone_checksum if checksums is None else checksums:
        args.append("--checksum")
    return args


async def spawn_argv(
    argv: list[str],
    log_path: Path,
    config_path: str,
    stats_interval: int | None = None,
    defaults: dict | None = None,
) -> asyncio.subprocess.Process:
    """Start any rclone operation.

    `argv` is `[operation, source, destination?, ...validated flags]` as built by
    `app.operations.build_argv`; `defaults` may override the app-level defaults
    for `checksums` / `transfers`. The caller must wait/cancel the process and
    unlink the config afterwards.
    """
    source = argv[1] if len(argv) > 1 else ""
    destination = argv[2] if len(argv) > 2 else ""
    args = [argv[0]]
    if source:
        args.append(await _expand_target(source))
    if destination:
        args.append(await _expand_target(destination))
    # Directory-valued flags must be expanded for `local` remotes exactly like
    # source/destination (rclone's local backend has no config-level root, so a
    # bare `remote:path` would resolve against the process working directory).
    rest = list(argv[3:])
    for i, token in enumerate(rest):
        if token in _DIR_VALUE_FLAGS and i + 1 < len(rest):
            rest[i + 1] = await _expand_target(rest[i + 1])
    args.extend(rest)
    args.extend(
        await global_flags(
            config_path,
            log_path,
            stats_interval,
            checksums=(defaults or {}).get("checksums"),
            transfers=(defaults or {}).get("transfers"),
        )
    )
    return await asyncio.create_subprocess_exec(
        settings.rclone_bin,
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )


async def spawn_transfer(
    source: str,
    destination: str,
    log_path: Path,
    config_path: str,
    mode: str = "sync",
    extra_args: list[str] | None = None,
) -> asyncio.subprocess.Process:
    """Start a transfer from a mode name.

    Thin wrapper over `spawn_argv`, kept for the backup/restore engines which
    pass a mode plus an already-built flag list (e.g. --backup-dir).
    """
    argv = [mode, source, destination]
    if extra_args:
        argv.extend(extra_args)
    return await spawn_argv(argv, log_path, config_path)


async def follow_log(log_path: Path, on_line, poll: float = 0.25) -> None:
    """Call `on_line(raw_line)` for every line appended to `log_path`.

    Turns rclone's JSON log into live progress while the process runs, instead
    of re-reading the whole file on every poll.
    """
    offset = 0
    pending = ""
    while True:
        try:
            with open(log_path, "rb") as handle:
                handle.seek(offset)
                chunk = handle.read()
                offset += len(chunk)
        except OSError:
            await asyncio.sleep(poll)
            continue
        if not chunk:
            await asyncio.sleep(poll)
            continue
        pending += chunk.decode("utf-8", "replace")
        *lines, pending = pending.split("\n")
        for line in lines:
            if line.strip():
                await on_line(line)


async def drain(proc: asyncio.subprocess.Process) -> None:
    """Consume stdout/stderr concurrently so neither pipe buffer can fill up
    and block the child process (a sequential read can deadlock if the other
    stream exceeds the OS pipe buffer while we are blocked on the first)."""
    async def _read(stream) -> None:
        if stream is not None:
            try:
                while await stream.read(65536):
                    pass
            except Exception:  # noqa: BLE001
                pass

    await asyncio.gather(_read(proc.stdout), _read(proc.stderr))


def tail(path: Path, lines: int = 40) -> str:
    """Return the last `lines` lines of a log without reading the whole file."""
    try:
        with path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 128 * 1024))
            data = f.read().decode("utf-8", "replace")
        return "\n".join(data.splitlines()[-lines:])
    except OSError:
        return ""