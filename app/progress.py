"""Structured transfer progress from rclone's JSON log.

Every run is spawned with ``--use-json-log``, so instead of scraping the
human-readable stats line with a regex we get a machine-readable ``stats``
object per interval:

    {"time": "...", "level": "info", "stats": {"bytes": 12582912,
     "totalBytes": 12582912, "speed": 1048576, "eta": null,
     "transfers": 4, "totalTransfers": 4, "errors": 0, "checks": 0,
     "totalChecks": 0, "deletes": 0, "deletedDirs": 0, "listed": 4,
     "elapsedTime": 0.017, ...}}

That drives the live progress bar, the History counters and the run summary.
Older runs (and any log written before this change) are plain text, so the text
fallback below is kept for them.
"""

import json
import re
from pathlib import Path

# Text stats line (rclone without --use-json-log), kept as a fallback:
#   Transferred:    12 MiB / 12 MiB, 100%, 1.5 MiB/s, ETA -
_TEXT_STATS_RE = re.compile(
    r"Transferred:\s+([\d.]+\s+\S+)\s*/\s*([\d.]+\s+\S+),\s*(\d+)%,\s*([\d.]+\s+\S+)"
)
_TEXT_COPIED_RE = re.compile(r": Copied \(")

# `rclone check` (and a few other commands) ignore --use-json-log and print a
# text summary instead, e.g. "Local file system: 1 files missing". Parse those
# so a verify run still reports real numbers in the UI.
_TEXT_SUMMARY_RE = re.compile(
    r"(\d+)\s+(matching files|files missing|differences found|errors while checking"
    r"|orphaned files|files deleted|directories deleted)",
    re.IGNORECASE,
)
_SUMMARY_KEYS = {
    "matching files": "matched",
    "files missing": "missing",
    "differences found": "differences",
    "errors while checking": "errors",
    "orphaned files": "orphaned",
    "files deleted": "deleted",
    "directories deleted": "deleted_dirs",
}

_UNITS = {
    "Bytes": 1,
    "B": 1,
    "KiB": 1024,
    "MiB": 1024**2,
    "GiB": 1024**3,
    "TiB": 1024**4,
}


def to_bytes(amount) -> int:
    """'12 MiB' -> 12582912 (unparsable input -> 0)."""
    try:
        value, unit = str(amount).split()
        return int(float(value) * _UNITS.get(unit, 1))
    except (ValueError, IndexError):
        return 0


def parse_line(raw):
    """One log line as a dict, or None when it is not a JSON log entry."""
    line = (raw or "").strip()
    if not line.startswith("{"):
        return None
    try:
        entry = json.loads(line)
    except json.JSONDecodeError:
        return None
    return entry if isinstance(entry, dict) else None


def progress_from_entry(entry):
    """Normalised progress dict from a JSON stats entry, or None."""
    stats = (entry or {}).get("stats")
    if not isinstance(stats, dict):
        return None
    total_bytes = int(stats.get("totalBytes") or 0)
    done_bytes = int(stats.get("bytes") or 0)
    total_transfers = int(stats.get("totalTransfers") or 0)
    transfers = int(stats.get("transfers") or 0)
    total_checks = int(stats.get("totalChecks") or 0)
    eta = stats.get("eta")
    percent = 0.0
    if total_bytes:
        percent = min(100.0, round(100 * done_bytes / total_bytes, 1))
    elif total_transfers:
        percent = min(100.0, round(100 * transfers / total_transfers, 1))
    elif total_checks:
        percent = min(100.0, round(100 * int(stats.get("checks") or 0) / total_checks, 1))
    return {
        "percent": percent,
        "bytes": done_bytes,
        "total_bytes": total_bytes,
        "remaining_bytes": max(0, total_bytes - done_bytes),
        "transfers": transfers,
        "total_transfers": total_transfers,
        "speed": float(stats.get("speed") or 0.0),
        "eta": float(eta) if isinstance(eta, (int, float)) else None,
        "checks": int(stats.get("checks") or 0),
        "total_checks": total_checks,
        "deletes": int(stats.get("deletes") or 0),
        "deleted_dirs": int(stats.get("deletedDirs") or 0),
        "listed": int(stats.get("listed") or 0),
        "renames": int(stats.get("renames") or 0),
        "server_side_copies": int(stats.get("serverSideCopies") or 0),
        "errors": int(stats.get("errors") or 0),
        "elapsed": float(stats.get("elapsedTime") or 0.0),
        "fatal": bool(stats.get("fatalError")),
    }


def message_from_entry(entry) -> str:
    """The `msg` field with embedded newlines collapsed to one line."""
    msg = str((entry or {}).get("msg") or "").strip()
    return " ".join(msg.split())


def humanize_line(raw: str) -> str:
    """Render one log line the way the old text log looked.

    Keeps the run-log viewer and the API's `{ts, level, message}` contract
    unchanged even though runs are now logged as JSON.
    """
    entry = parse_line(raw)
    if entry is None:
        return raw
    stamp = str(entry.get("time") or "")
    ts = stamp[:19].replace("T", " ") if "T" in stamp else ""
    level = str(entry.get("level") or "info").upper()
    message = message_from_entry(entry)
    name = entry.get("object")
    if name:
        message = f"{message}: {name}" if message else str(name)
    if not message and entry.get("stats"):
        message = "stats"
    return f"{ts} {level}: {message}" if ts else message


def empty_progress() -> dict:
    """A zeroed progress payload (also the shape the API/UI expects)."""
    return {
        "percent": 0.0,
        "bytes": 0,
        "total_bytes": 0,
        "remaining_bytes": 0,
        "transfers": 0,
        "total_transfers": 0,
        "speed": 0.0,
        "eta": None,
        "checks": 0,
        "total_checks": 0,
        "deletes": 0,
        "deleted_dirs": 0,
        "listed": 0,
        "renames": 0,
        "server_side_copies": 0,
        "errors": 0,
        "elapsed": 0.0,
        "fatal": False,
        "files": 0,
        "deletes_size": 0,
        "matched": 0,
        "missing": 0,
        "differences": 0,
        "orphaned": 0,
    }


def _progress_from_text(raw_log: str):
    """Best-effort progress from a legacy text stats line."""
    progress = None
    for line in raw_log.splitlines():
        match = _TEXT_STATS_RE.search(line)
        if match:
            progress = empty_progress()
            progress["bytes"] = to_bytes(match.group(1))
            progress["total_bytes"] = to_bytes(match.group(2))
            progress["speed"] = to_bytes(match.group(4))
            try:
                progress["percent"] = float(match.group(3))
            except ValueError:
                pass
            progress["remaining_bytes"] = max(
                0, progress["total_bytes"] - progress["bytes"]
            )
    return progress


def _summary_counts(text: str) -> dict:
    """Extract rclone's check/delete summary counters from a message.

    Works for both formats: plain text lines (`2026/… NOTICE: 1 files missing`)
    and JSON log entries (`{"level":"notice","msg":"1 files missing"}`).
    """
    out: dict = {}
    for count, label in _TEXT_SUMMARY_RE.findall(text or ""):
        key = _SUMMARY_KEYS.get(label.lower())
        if key:
            out[key] = max(out.get(key, 0), int(count))
    return out


def parse_log(raw_log: str) -> dict:
    """Scan a log and return the final progress plus counters.

    Handles JSON logs (this app's runs) and legacy text logs alike.
    """
    last = None
    errors = checks = deletes = deleted_dirs = listed = 0
    summary: dict = {}
    files = copied_lines = 0
    total_bytes_seen = 0

    for raw in (raw_log or "").splitlines():
        entry = parse_line(raw)
        if entry is None:
            # Legacy text log: count what we can and parse check summaries.
            if _TEXT_COPIED_RE.search(raw):
                copied_lines += 1
            if "ERROR" in raw or "Failed to" in raw:
                errors += 1
            for key, count in _summary_counts(raw).items():
                summary[key] = max(summary.get(key, 0), count)
            continue

        progress = progress_from_entry(entry)
        if progress:
            last = progress
            continue

        level = str(entry.get("level") or "").lower()
        msg = message_from_entry(entry)
        # rclone check / delete report results as notices ("1 files missing",
        # "3 differences found", …); read them from the message too.
        for key, count in _summary_counts(msg).items():
            summary[key] = max(summary.get(key, 0), count)
        if level == "error" or msg.startswith("Failed to") or msg.startswith("Error"):
            errors += 1
        elif msg.startswith("Deleted directories"):
            deleted_dirs += 1
        elif msg.startswith("Deleted"):
            deletes += 1
        elif msg.startswith("Listed"):
            listed += 1
        elif msg.startswith(("Copied", "Moved", "Transferred")):
            files += 1
        if isinstance(entry.get("size"), (int, float)):
            total_bytes_seen = max(total_bytes_seen, int(entry["size"]))

    if last is None:
        last = _progress_from_text(raw_log) or empty_progress()

    # Final numbers come from the last stats snapshot; counters that only exist
    # as log lines (and the text fallback) are merged in without lowering them.
    if last.get("transfers"):
        files = max(files, last["transfers"])
    else:
        files = max(files, copied_lines)
    size = max(total_bytes_seen, last.get("bytes") or 0)
    last["files"] = files
    last["bytes"] = size
    last["errors"] = max(errors, last.get("errors") or 0)
    last["checks"] = max(checks, last.get("checks") or 0)
    last["deletes"] = max(deletes, last.get("deletes") or 0)
    last["deleted_dirs"] = max(deleted_dirs, last.get("deleted_dirs") or 0)
    last["listed"] = max(listed, last.get("listed") or 0)
    last["remaining_bytes"] = max(
        0, (last.get("total_bytes") or 0) - size
    )
    if not last.get("speed") and last.get("elapsed") and size:
        last["speed"] = size / last["elapsed"]
    # Verify/deletion summaries (rclone check prints them as text).
    for key in ("matched", "missing", "differences", "orphaned"):
        last[key] = summary.get(key, 0)
    last["errors"] = max(last.get("errors") or 0, summary.get("errors", 0))
    last["deletes"] = max(last.get("deletes") or 0, summary.get("deleted", 0))
    last["deleted_dirs"] = max(last.get("deleted_dirs") or 0, summary.get("deleted_dirs", 0))
    # A check that compared files has no byte totals; show it as complete.
    if not last.get("total_bytes") and (last["matched"] or last["missing"] or last["differences"]):
        checked = last["matched"] + last["missing"] + last["differences"]
        last["total_transfers"] = max(last.get("total_transfers") or 0, checked)
        last["transfers"] = last["matched"]
        last["total_checks"] = max(last.get("total_checks") or 0, checked)
        last["checks"] = last["matched"]
        last["percent"] = 100.0
    return last


def read_progress(path, tail_bytes: int = 256 * 1024) -> dict:
    """Progress of a finished or in-flight run, read from its log file.

    Only the last `tail_bytes` are read so a multi-GB log cannot stall the
    event loop or the UI request.
    """
    if not path:
        return empty_progress()
    target = Path(path)
    try:
        if not target.exists():
            return empty_progress()
        size = target.stat().st_size
        with target.open("rb") as handle:
            if size > tail_bytes:
                handle.seek(size - tail_bytes)
            data = handle.read().decode("utf-8", "replace")
    except OSError:
        return empty_progress()
    return parse_log(data)