"""Canonical S3/remote target path helpers.

Targets are expressed in the logical form ``remote:bucket/prefix/...`` (the
same shape the rest of the app uses, e.g. ``s3-remote:my-bucket``). The
physical endpoint of ``local`` remotes is inlined at the rclone boundary by
``app.rclone._expand_target``; these helpers never see it.

All helpers are tolerant of optional leading/trailing slashes and of an
already-suffixed ``/latest`` path, e.g. all of these are equivalent:

    get_latest_path("s3:my-bucket")        == "s3:my-bucket/latest"
    get_latest_path("s3:my-bucket/latest") == "s3:my-bucket/latest"
    get_latest_path("s3:my-bucket/latest/")== "s3:my-bucket/latest"
"""

from datetime import datetime, timezone
import re

_TS_FORMAT = "%Y-%m-%d_%H-%M-%S"
_ARCHIVE_TS_RE = re.compile(r"/archive/(\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})/?$")


def split_target(target: str) -> tuple[str, list[str]]:
    """Split ``remote:bucket/a/b`` into (remote, [bucket, a, b])."""
    remote, _, path = (target or "").partition(":")
    segs = [s for s in path.split("/") if s]
    return remote, segs


def _join(remote: str, segs: list[str]) -> str:
    return f"{remote}:" + ("/".join(segs) if segs else "")


def get_base_target(target: str) -> str:
    """Strip trailing slashes and any trailing ``/latest`` subpath to yield the
    root bucket/prefix path.

    >>> get_base_target("s3-remote:my-bucket/latest/")
    's3-remote:my-bucket'
    >>> get_base_target("s3-remote:my-bucket")
    's3-remote:my-bucket'
    """
    remote, segs = split_target(target)
    if segs and segs[-1] == "latest":
        segs = segs[:-1]
    return _join(remote, segs)


def get_latest_path(target: str) -> str:
    """Return the clean ``/latest`` mirror path for a target (idempotent)."""
    remote, segs = split_target(get_base_target(target))
    if not segs or segs[-1] != "latest":
        segs.append("latest")
    return _join(remote, segs)


def get_archive_path(target: str, timestamp: str) -> str:
    """Return the ``/archive/<timestamp>`` path under the target's base path.

    >>> get_archive_path("s3-remote:my-bucket/latest", "2026-08-22_10-30-00")
    's3-remote:my-bucket/archive/2026-08-22_10-30-00'
    """
    remote, segs = split_target(get_base_target(target))
    segs += ["archive"] + [s for s in str(timestamp).strip("/").split("/") if s]
    return _join(remote, segs)


def is_latest_target(target: str) -> bool:
    """True when the target path contains a ``latest`` segment."""
    _, segs = split_target(target)
    return "latest" in segs


def timestamp_now() -> str:
    """Current UTC wall-clock as an archive delta name: YYYY-MM-DD_HH-MM-SS."""
    return datetime.now(timezone.utc).strftime(_TS_FORMAT)


def parse_archive_stamp(name: str) -> datetime | None:
    """Parse an archive delta folder name to an aware UTC datetime, or None."""
    try:
        return datetime.strptime(name, _TS_FORMAT).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def normalize_restore_source(raw_source: str) -> tuple[str, str | None]:
    """Normalize a restore ``source`` target into ``(base_remote, timestamp)``.

    ``base_remote`` is the root bucket/prefix path (e.g. ``backup-s3:backup-data``);
    ``timestamp`` is ``"latest"`` for the current mirror, the extracted
    ``YYYY-MM-DD_HH-MM-SS`` for a historical archive, or ``None`` when the input
    is empty/ambiguous.

    Handles every shape the UI/API may pass in:

    >>> normalize_restore_source("backup-s3:backup-data/archive/2026-08-22_07-40-45")
    ('backup-s3:backup-data', '2026-08-22_07-40-45')
    >>> normalize_restore_source("backup-s3:backup-data/archive/2026-08-22_07-40-45/")
    ('backup-s3:backup-data', '2026-08-22_07-40-45')
    >>> normalize_restore_source("backup-s3:backup-data/latest")
    ('backup-s3:backup-data', 'latest')
    >>> normalize_restore_source("backup-s3:backup-data/latest/")
    ('backup-s3:backup-data', 'latest')
    >>> normalize_restore_source("backup-s3:backup-data")
    ('backup-s3:backup-data', 'latest')
    """
    raw = (raw_source or "").strip()
    if not raw:
        return "", None
    m = _ARCHIVE_TS_RE.search(raw)
    if m:
        ts = m.group(1)
        base_remote = raw[: m.start()]
        return base_remote, ts
    # No archive timestamp: treat the source as the current /latest/ mirror.
    return get_base_target(raw), "latest"
