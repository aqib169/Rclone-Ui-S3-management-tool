"""Point-in-time restore engine.

``latest`` restore: a single copy of ``/latest/`` into the target.

Historical restore of timestamp ``T``: copy ``/latest/`` into the target, then
overlay every ``/archive/<d>/`` delta created AT or AFTER ``T`` in
reverse-chronological order (newest first). Each delta holds the pre-run
versions of the files that run changed, so overlaying it reverts that run's
changes; including ``T``'s own delta reverts the changes run ``T`` made, giving
the state as it was just before run ``T``. Newest-first ordering keeps the
earliest snapshot when multiple deltas touch the same file.

Note: overlays use ``rclone copy`` (additive, never deletes), so files created
by runs after ``T`` remain in the target. This is a documented limitation of a
delta layout that only stores the changed files.
"""

import asyncio
import os

from sqlalchemy import select

from app.config import settings
from app.database import SessionLocal
from app.models import Run, target_str, utcnow
from app.rclone import archive_dates, drain, spawn_transfer, tail, write_temp_config
from app.services.locks import release
from app.services.common import (
    append_log,
    mark_restore_failed,
    mark_run,
    parse_transfer_summary,
)
from app.state import active_runs, restore_jobs
from app.utils.path_utils import (
    get_base_target,
    get_latest_path,
    normalize_restore_source,
    split_target,
)


async def _pitr_overlay_sources(base_remote: str, ts: str) -> list[str]:
    """Reverse-Overlay sources for a historical restore of timestamp ``T``
    against ``base_remote`` (e.g. ``backup-s3:backup-data``).

    Returns the ``/latest/`` base layer first, then every ``/archive/<d>/``
    delta created AT or AFTER ``T``, newest first. Including ``T``'s own delta
    reverts the changes that run ``T`` made.
    """
    remote, segs = split_target(get_base_target(base_remote))
    bucket = "/".join(segs)
    steps = [get_latest_path(base_remote)]
    for d in await archive_dates(remote, bucket):
        if d >= ts:
            steps.append(target_str(remote, bucket, f"archive/{d}"))
    return steps


def default_restore_target(source_bucket: str, stamp: str) -> str:
    """Default target path when the caller does not specify one:
    ``<source_bucket>-restored-<stamp>`` (e.g. ``my-bucket-restored-2026-08-22_10-30-00``)."""
    name = (source_bucket or "restored").strip("/")
    return f"{name}-restored-{stamp}"


async def execute_restore(run_id: str) -> None:
    """Run a single restore run (latest or historical point-in-time)."""
    async with SessionLocal() as db:
        run = (await db.execute(select(Run).where(Run.run_id == run_id))).scalar_one_or_none()
        if run is None:
            return
        cfg = restore_jobs.get(run_id)
        if cfg is None:
            return
        paths = list(cfg.get("paths") or [])
        mode = cfg.get("mode", "files")
        archive, dest_root = run.source, run.destination
        log_path = settings.logs_dir / f"run_{run_id}.log"
        run.status = "running"
        run.started_at = utcnow()
        run.log_path = str(log_path)
        await db.commit()

    # Normalize whatever the UI/API passed as `source`: the routers send the
    # canonical target (``remote:bucket/archive/<ts>`` or ``remote:bucket/latest``),
    # but the engine must also accept a full ``remote:bucket/archive/<ts>`` path
    # and never scan ``.../archive/<ts>/archive/``.
    base_remote, target_timestamp = normalize_restore_source(archive)
    if not base_remote:
        release(run_id)
        await mark_run(
            run_id,
            status="failed",
            finished_at=utcnow(),
            exit_code=-1,
            error=f"invalid restore source '{archive}'",
        )
        return

    config_path = None
    failed: list[str] = []
    ok = True
    try:
        config_path = await write_temp_config()
        active_runs[run_id] = {
            "proc": None,
            "kind": "restore",
            "source": archive,
            "destination": dest_root,
            "job_name": "(restore)",
        }
        append_log(log_path, f"=== restore {mode} {archive} -> {dest_root} ===")
        if mode == "all":
            if target_timestamp in (None, "latest"):
                # Restore `latest`: single copy from the /latest/ baseline.
                latest = get_latest_path(base_remote)
                append_log(log_path, f"restore latest: copy {latest} -> {dest_root}")
                proc = await spawn_transfer(latest, dest_root, log_path, config_path, mode="copy")
                active_runs[run_id]["proc"] = proc
                code = await proc.wait()
                await drain(proc)
                if code != 0:
                    ok = False
                    failed = ["<latest>"]
            else:
                # Historical point-in-time restore to timestamp T — strict
                # Reverse-Overlay sequence:
                #   Step A (base /latest/): copy the unchanged baseline.
                #   Step B (overlay archive i/N): copy each post-T delta ->
                #       target, newest first. Each delta holds the pre-run
                #       versions, so post-T edits are reverted and files
                #       deleted after T are put back.
                append_log(log_path, f"PITR: restore {archive} -> {dest_root} (reverse overlay)")
                sources = await _pitr_overlay_sources(base_remote, target_timestamp)
                if not sources:
                    raise RuntimeError(f"cannot reconstruct point-in-time from {archive}")
                latest, deltas = sources[0], sources[1:]
                append_log(
                    log_path,
                    f"Found {len(deltas)} overlay archive(s) to apply for timestamp {target_timestamp}",
                )
                append_log(log_path, f"--- Step A (base /latest/): copy {latest} -> {dest_root}")
                proc = await spawn_transfer(latest, dest_root, log_path, config_path, mode="copy")
                active_runs[run_id]["proc"] = proc
                code = await proc.wait()
                await drain(proc)
                if code != 0:
                    ok = False
                    failed = [f"<{latest}>"]
                for i, delta in enumerate(deltas, start=1):
                    append_log(
                        log_path,
                        f"--- Step B (overlay {i}/{len(deltas)}): copy {delta} -> {dest_root}",
                    )
                    proc = await spawn_transfer(delta, dest_root, log_path, config_path, mode="copy")
                    active_runs[run_id]["proc"] = proc
                    code = await proc.wait()
                    await drain(proc)
                    if code != 0:
                        ok = False
                        failed = [f"<{delta}>"]
                        break
        else:
            for item in paths:
                entry = active_runs.get(run_id)
                if entry and entry.get("cancelled"):
                    ok = False
                    break
                p = item.get("path", "")
                is_dir = bool(item.get("is_dir"))
                append_log(log_path, f"--- restoring {p} ---")
                src = f"{archive}/{p}"
                # dirs: copy contents into <dest>/<p>; files: copy into <dest>/<dirname(p)>
                dst = f"{dest_root}/{p}" if is_dir else f"{dest_root}/{os.path.dirname(p)}"
                proc = await spawn_transfer(src, dst, log_path, config_path, mode="copy")
                if active_runs.get(run_id):
                    active_runs[run_id]["proc"] = proc
                code = await proc.wait()
                await drain(proc)
                if code != 0:
                    failed.append(p)
                    ok = False
    except Exception as exc:  # noqa: BLE001
        ok = False
        failed = [str(exc)]
    finally:
        if config_path:
            try:
                os.unlink(config_path)
            except OSError:
                pass

    entry = active_runs.pop(run_id, None)
    restore_jobs.pop(run_id, None)
    release(run_id)  # the destination is free again
    if entry and entry.get("cancelled"):
        await mark_run(run_id, status="cancelled", finished_at=utcnow())
        return
    status = "success" if ok else "failed"
    error = "" if ok else "failed paths: " + ", ".join(failed)
    files, size = parse_transfer_summary(tail(log_path, lines=400))
    if failed:
        await mark_restore_failed(run_id, failed)
    await mark_run(
        run_id,
        status=status,
        finished_at=utcnow(),
        exit_code=0 if ok else 1,
        error=error,
        files=files,
        size_bytes=size,
    )
