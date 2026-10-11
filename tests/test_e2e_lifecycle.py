#!/usr/bin/env python3
"""Comprehensive pytest end-to-end suite for the backup/restore orchestrator.

Boots a real uvicorn instance against an isolated SQLite data dir and validates
the full lifecycle against local directory remotes:

  * Baseline sync -> delta sync -> file modification & deletion -> correct
    archive generation (old versions + deleted files, no full duplicate).
  * Point-in-time restores: ``latest`` and a historical timestamp (reverse
    overlay of deltas created after the target).
  * Retention pruning of archive deltas older than RETAIN_DAYS.
  * Cron validation logic (step-hour auto-sanitization + presets).

Run:
    .venv/bin/python -m pytest tests/test_e2e_lifecycle.py -v
"""

import asyncio
import base64
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

RCLONE_BIN = os.environ.get("RCLONE_BIN", "/usr/bin/rclone")
USERNAME = "admin"
PASSWORD = "12345678"

_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}$")

FILE1_V1 = b"file1-original-v1\n"
FILE1_V2 = b"file1-modified-version-2\n"
FILE2 = b"file2\n"
FILE3_V1 = b"file3-original\n"
FILE3_V2 = b"file3-modified-v2\n"
FILE4 = b"file4\n"
FILE5 = b"file5\n"


class Orch:
    """Thin JSON API client + local file helpers for the booted orchestrator."""

    def __init__(self, base_url: str, src: Path, dst: Path, rt: Path, data_dir: Path, proc):
        self.base_url = base_url
        self.src = src
        self.dst = dst
        self.rt = rt
        self.data_dir = data_dir
        self.proc = proc

    def _auth(self) -> str:
        token = base64.b64encode(f"{USERNAME}:{PASSWORD}".encode()).decode()
        return "Basic " + token

    def api(self, method: str, path: str, payload=None, timeout: int = 30):
        headers = {"Authorization": self._auth()}
        body = None
        if payload is not None:
            body = json.dumps(payload).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base_url + path, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, resp.read().decode()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode()

    def json(self, method: str, path: str, payload=None):
        status, body = self.api(method, path, payload)
        try:
            return status, json.loads(body) if body and body.strip() else None
        except json.JSONDecodeError:
            return status, body

    def create_remote(self, name: str, path: Path) -> int:
        status, _ = self.json(
            "POST",
            "/api/remotes",
            {"name": name, "type": "local", "endpoint": str(path), "access_key": "x", "secret_key": "y"},
        )
        return status

    def create_job(self, name: str, schedule: str = "") -> int:
        status, data = self.json(
            "POST",
            "/api/jobs",
            {
                "name": name,
                "source_remote": "src",
                "source_bucket": "",
                "source_path": "folder",
                "dest_remote": "dst",
                "dest_bucket": "mybucket",
                "dest_path": "",
                "schedule_cron": schedule,
                "enabled": True,
            },
        )
        assert status == 201, f"create job failed: {data}"
        return data["id"]

    def run_job(self, job_id: int) -> str:
        status, data = self.json("POST", f"/api/jobs/{job_id}/run")
        assert status == 202, f"run job failed: {data}"
        return data["run_id"]

    def wait_run(self, run_id: str, timeout: int = 120) -> str:
        deadline = time.time() + timeout
        last = "pending"
        while time.time() < deadline:
            status, data = self.json("GET", f"/api/runs/{run_id}/status")
            if status == 200 and isinstance(data, dict):
                last = data.get("status", last)
                if last in ("success", "failed", "cancelled"):
                    return last
            time.sleep(0.4)
        return last

    def restore(self, archive_date: str, target_path: str = "", target_bucket: str = "") -> str:
        status, data = self.json(
            "POST",
            "/api/restore",
            {
                "source_remote": "dst",
                "source_bucket": "mybucket",
                "archive_date": archive_date,
                "mode": "all",
                "target_remote": "rt",
                "target_bucket": target_bucket,
                "target_path": target_path,
            },
        )
        assert status == 202, f"restore failed: {data}"
        return data["run_id"]

    @staticmethod
    def files_under(root: Path) -> dict[str, bytes]:
        root = Path(root)
        if not root.exists():
            return {}
        return {
            str(p.relative_to(root)): p.read_bytes()
            for p in sorted(root.rglob("*"))
            if p.is_file()
        }

    @staticmethod
    def total_bytes(root: Path) -> int:
        root = Path(root)
        if not root.exists():
            return 0
        return sum(p.stat().st_size for p in root.rglob("*") if p.is_file())

    @staticmethod
    def write(path: Path, content: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    def run_operation(self, operation: str, source: str, destination: str = "", flags=None):
        """Start an ad-hoc rclone operation from the Transfer Console API."""
        payload = {"operation": operation, "source": source, "flags": flags or {}}
        if destination:
            payload["destination"] = destination
        status, data = self.json("POST", "/api/transfers/run", payload)
        assert status == 202, f"{operation} failed to start: {data}"
        return data["run_id"]

    def progress(self, run_id: str) -> dict:
        status, data = self.json("GET", f"/api/runs/{run_id}/progress")
        assert status == 200, f"progress failed: {data}"
        return data

    def html(self, path: str) -> tuple:
        req = urllib.request.Request(self.base_url + path, headers={"Authorization": self._auth()})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.status, resp.read().decode()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode()

    @staticmethod
    def archive_stamps(dst: Path) -> list[str]:
        archive_dir = dst / "mybucket" / "archive"
        if not archive_dir.exists():
            return []
        return sorted(p.name for p in archive_dir.iterdir() if p.is_dir() and _TS_RE.match(p.name))


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def orch(tmp_path_factory):
    work = tmp_path_factory.mktemp("garage-e2e")
    src, dst, rt = work / "src", work / "dst", work / "rt"
    data_dir = work / "data"
    for d in (src, dst, rt):
        d.mkdir(parents=True, exist_ok=True)

    # The TEST process's app imports must see the same data dir the server uses.
    # Another test module may already have imported `app.*` with a different
    # DATA_DIR, so re-point the imported singletons at this run's directory.
    os.environ["DATA_DIR"] = str(data_dir)
    os.environ["RCLONE_BIN"] = RCLONE_BIN
    if "app.config" in sys.modules:
        # `app.config` may already be imported (by another test module) with a
        # different DATA_DIR; settings is a singleton, so re-point it here.
        from app import config as _config

        _config.settings.data_dir = data_dir
    if "app.database" in sys.modules:
        from app import database as _database
        engine = _database.create_async_engine(
            _config.settings.sqlite_url, echo=False, pool_pre_ping=True
        )
        session_factory = _database.async_sessionmaker(
            engine, class_=_database.AsyncSession, expire_on_commit=False
        )
        _database.engine = engine
        _database.SessionLocal = session_factory
        # Every module that did `from app.database import SessionLocal` holds its
        # own reference; point them all at the new engine.
        for module in list(sys.modules.values()):
            name = getattr(module, "__name__", "")
            if name.startswith("app.") and getattr(module, "SessionLocal", None) is not None:
                module.SessionLocal = session_factory
        crypto = sys.modules.get("app.crypto")
        if crypto is not None:
            crypto._key_path = data_dir / ".encryption_key"

    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    server_env = dict(os.environ)
    server_env.update(
        {
            "DATA_DIR": str(data_dir),
            "USERNAME": USERNAME,
            "PASSWORD": PASSWORD,
            "RCLONE_BIN": RCLONE_BIN,
            "RETAIN_DAYS": "2",
        }
    )
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(REPO),
        env=server_env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    client = Orch(base, src, dst, rt, data_dir, proc)
    try:
        deadline = time.time() + 30
        while time.time() < deadline:
            if proc.poll() is not None:
                raise RuntimeError("orchestrator exited during startup")
            try:
                status, _ = client.api("GET", "/health", timeout=2)
                if status == 200:
                    break
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0.4)
        else:
            raise RuntimeError("orchestrator did not become healthy")
        for name, path in (("src", src), ("dst", dst), ("rt", rt)):
            assert client.create_remote(name, path) in (201, 409), f"remote {name} failed"
        yield client
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)
        shutil.rmtree(work, ignore_errors=True)


# ---------------------------------------------------------------- 1. baseline
def test_baseline_sync_creates_latest_without_deltas(orch: Orch):
    for name, content in (("file1.txt", FILE1_V1), ("file2.txt", FILE2), ("file3.txt", FILE3_V1)):
        orch.write(orch.src / "folder" / name, content)

    job_id = orch.create_job("job1")
    run_id = orch.run_job(job_id)
    assert orch.wait_run(run_id) == "success"

    latest = orch.files_under(orch.dst / "mybucket" / "latest")
    assert latest == {
        "file1.txt": FILE1_V1,
        "file2.txt": FILE2,
        "file3.txt": FILE3_V1,
    }
    archive_dir = orch.dst / "mybucket" / "archive"
    assert not archive_dir.exists() or not any(archive_dir.iterdir())

    _, runs = orch.json("GET", f"/api/runs?job_id={job_id}&kind=backup")
    assert runs["runs"][0]["status"] == "success"


# ---------------------------------------------------------------- 2. delta sync
def test_delta_sync_generates_archive_of_old_versions(orch: Orch):
    orch.write(orch.src / "folder" / "file1.txt", FILE1_V2)  # modified
    (orch.src / "folder" / "file2.txt").unlink()              # deleted
    orch.write(orch.src / "folder" / "file4.txt", FILE4)      # added

    run_id = orch.run_job(1)
    assert orch.wait_run(run_id) == "success"

    latest = orch.files_under(orch.dst / "mybucket" / "latest")
    assert latest["file1.txt"] == FILE1_V2
    assert latest["file3.txt"] == FILE3_V1
    assert latest["file4.txt"] == FILE4
    assert "file2.txt" not in latest

    stamps = orch.archive_stamps(orch.dst)
    assert len(stamps) == 1
    ts2 = stamps[0]

    delta = orch.files_under(orch.dst / "mybucket" / "archive" / ts2)
    assert delta["file1.txt"] == FILE1_V1, "delta must hold the OLD version"
    assert delta["file2.txt"] == FILE2, "delta must hold the DELETED file"
    assert "file3.txt" not in delta and "file4.txt" not in delta

    # storage grew only by the modified/deleted files, not a full duplicate
    expected = orch.total_bytes(orch.dst / "mybucket" / "latest") + orch.total_bytes(
        orch.dst / "mybucket" / "archive" / ts2
    )
    assert orch.total_bytes(orch.dst) == expected
    two_full_copies = len(FILE1_V1) + len(FILE2) + len(FILE3_V1) + len(FILE1_V2) + len(FILE3_V1) + len(FILE4)
    assert expected < two_full_copies


# ---------------------------------------------------------------- 3. third run for PITR
def test_third_run_creates_second_delta(orch: Orch):
    time.sleep(1.2)  # guarantee a distinct timestamp for the new delta
    orch.write(orch.src / "folder" / "file3.txt", FILE3_V2)  # modified
    (orch.src / "folder" / "file4.txt").unlink()             # deleted AFTER ts2
    orch.write(orch.src / "folder" / "file5.txt", FILE5)     # added

    run_id = orch.run_job(1)
    assert orch.wait_run(run_id) == "success"

    stamps = orch.archive_stamps(orch.dst)
    assert len(stamps) == 2, stamps
    ts3 = stamps[-1]
    delta = orch.files_under(orch.dst / "mybucket" / "archive" / ts3)
    assert delta["file3.txt"] == FILE3_V1, "delta must hold the pre-run-3 version"
    assert delta["file4.txt"] == FILE4, "delta must hold the file deleted in run 3"

    latest = orch.files_under(orch.dst / "mybucket" / "latest")
    assert latest["file3.txt"] == FILE3_V2
    assert latest["file5.txt"] == FILE5
    assert "file4.txt" not in latest


# ---------------------------------------------------------------- 4. restores
def test_restore_latest_matches_current_state(orch: Orch):
    run_id = orch.restore("latest", target_path="restore-latest")
    assert orch.wait_run(run_id) == "success"
    restored = orch.files_under(orch.rt / "restore-latest")
    assert restored == {
        "file1.txt": FILE1_V2,
        "file3.txt": FILE3_V2,
        "file5.txt": FILE5,
    }


def test_restore_historical_point_in_time_reverse_overlay(orch: Orch):
    ts2 = orch.archive_stamps(orch.dst)[0]

    run_id = orch.restore(ts2, target_path="restore-pitr")
    assert orch.wait_run(run_id) == "success"

    _, logs = orch.json("GET", f"/api/runs/{run_id}/logs")
    log_text = " | ".join(l.get("message", "") for l in (logs or {}).get("lines", []))
    assert f"Found 2 overlay archive(s) to apply for timestamp {ts2}" in log_text, log_text
    assert "--- Step A (base /latest/): copy dst:mybucket/latest -> rt:restore-pitr" in log_text, log_text
    assert "--- Step B (overlay 1/2): copy dst:mybucket/archive/" in log_text, log_text
    assert "--- Step B (overlay 2/2): copy dst:mybucket/archive/" in log_text, log_text

    recovered = orch.files_under(orch.rt / "restore-pitr")
    # Reverse-overlay to ts2 with >= semantics: Step A copies /latest/, then
    # both post-ts2 deltas are applied newest-first — ts3 (f3 v1, f4) reverting
    # run-3's changes, then ts2 (f1 v1, file2) reverting run-2's changes.
    assert recovered["file1.txt"] == FILE1_V1, "run-2 edit must be reverted by the ts2 delta"
    assert recovered["file2.txt"] == FILE2, "file deleted in run 2 must be restored by the ts2 delta"
    assert recovered["file3.txt"] == FILE3_V1, "run-3 edit must be reverted by the ts3 delta"
    assert recovered["file4.txt"] == FILE4, "file deleted in run 3 must be restored by the ts3 delta"
    # Additive copy-overlay never deletes: file5 (added in run 3) lingers.
    assert recovered.get("file5.txt") == FILE5


def test_restore_historical_to_live_bucket(orch: Orch):
    """PITR straight into a live bucket (e.g. staging:staging-media): Step A
    recovers the unchanged /latest/ baseline and Step B overlays every delta at
    or after T, so the live bucket ends up with untouched files AND reverted /
    deleted files."""
    ts2 = orch.archive_stamps(orch.dst)[0]

    run_id = orch.restore(ts2, target_bucket="live-media")
    assert orch.wait_run(run_id) == "success"

    live = orch.files_under(orch.rt / "live-media")
    assert live["file1.txt"] == FILE1_V1, "run-2 edit reverted into the live bucket"
    assert live["file2.txt"] == FILE2, "file deleted in run 2 restored into the live bucket"
    assert live["file3.txt"] == FILE3_V1, "run-3 edit reverted into the live bucket"
    assert live["file4.txt"] == FILE4, "file deleted in run 3 restored into the live bucket"
    assert live.get("file5.txt") == FILE5


def test_restore_without_target_uses_default_target_path(orch: Orch):
    # Default target = <source_bucket>-restored-<stamp> on the same remote.
    status, data = orch.json(
        "POST",
        "/api/restore",
        {
            "source_remote": "dst",
            "source_bucket": "mybucket",
            "archive_date": "latest",
            "mode": "all",
            "target_remote": "dst",
            "target_bucket": "",
            "target_path": "",
        },
    )
    assert status == 202, data
    run_id = data["run_id"]
    assert orch.wait_run(run_id) == "success"

    default_dirs = [p for p in (orch.dst).iterdir() if p.is_dir() and "-restored-" in p.name]
    assert len(default_dirs) == 1, [p.name for p in (orch.dst).iterdir()]
    restored = orch.files_under(default_dirs[0])
    assert restored["file1.txt"] == FILE1_V2


# ---------------------------------------------------------------- 5. retention
def test_retention_prunes_archives_older_than_retain_days(orch: Orch):
    old = orch.dst / "mybucket" / "archive" / "2020-01-01_00-00-00" / "old.txt"
    orch.write(old, b"old")
    latest_before = orch.files_under(orch.dst / "mybucket" / "latest")

    from app.services.retention import prune_archives  # noqa: E402

    purged = asyncio.run(prune_archives("dst", "mybucket", 2))

    assert "2020-01-01_00-00-00" in purged
    assert not old.parent.exists(), "expired archive delta must be purged from disk"
    assert len(orch.archive_stamps(orch.dst)) == 2, "recent deltas must be retained"
    assert orch.files_under(orch.dst / "mybucket" / "latest") == latest_before


# ---------------------------------------------------------------- 6. cron
def test_normalize_restore_source_parses_every_target_shape():
    from app.utils.path_utils import normalize_restore_source  # noqa: E402

    assert normalize_restore_source("backup-s3:backup-data/archive/2026-08-22_07-40-45") == (
        "backup-s3:backup-data",
        "2026-08-22_07-40-45",
    )
    assert normalize_restore_source("backup-s3:backup-data/archive/2026-08-22_07-40-45/") == (
        "backup-s3:backup-data",
        "2026-08-22_07-40-45",
    )
    assert normalize_restore_source("backup-s3:backup-data/latest") == ("backup-s3:backup-data", "latest")
    assert normalize_restore_source("backup-s3:backup-data/latest/") == ("backup-s3:backup-data", "latest")
    assert normalize_restore_source("backup-s3:backup-data") == ("backup-s3:backup-data", "latest")
    assert normalize_restore_source("") == ("", None)


def test_cron_validation_sanitizes_step_hours_and_supports_presets():
    from apscheduler.triggers.cron.expressions import AllExpression  # noqa: E402

    from app.scheduler import build_cron_trigger  # noqa: E402
    from app.schemas import validate_cron_schedule  # noqa: E402

    presets = {
        "1h": "0 * * * *",
        "3h": "0 */3 * * *",
        "6h": "0 */6 * * *",
        "12h": "0 */12 * * *",
        "24h": "0 0 * * *",
    }
    for label, cron in presets.items():
        assert validate_cron_schedule(cron) == cron, label

    # auto-sanitize minute `*` when hours are stepped
    assert validate_cron_schedule("* */3 * * *") == "0 */3 * * *"
    assert validate_cron_schedule("* */6 * * *") == "0 */6 * * *"
    # untouched when minute is pinned or hour is not stepped
    assert validate_cron_schedule("0 */3 * * *") == "0 */3 * * *"
    assert validate_cron_schedule("* * * * *") == "* * * * *"

    # build_cron_trigger must never register a wildcard-minute step-hour trigger
    t = build_cron_trigger("* */3 * * *")
    minute = t.fields[6].expressions[0]
    hour = t.fields[5].expressions[0]
    assert type(minute) is not AllExpression and minute.first == 0
    assert hour.step == 3

    t = build_cron_trigger("0 */3 * * *")
    assert t.fields[6].expressions[0].first == 0 and t.fields[5].expressions[0].step == 3

    # invalid expressions rejected
    with pytest.raises(ValueError):
        validate_cron_schedule("61 * * * *")
    with pytest.raises(ValueError):
        validate_cron_schedule("0 */3 * *")



# ------------------------------------------------- transfer console + rclone surface
def test_provider_catalogue_is_exposed_and_drives_the_remote_form(orch: Orch):
    status, data = orch.json("GET", "/api/providers")
    assert status == 200 and data["count"] > 20, data
    names = {p["name"] for p in data["providers"]}
    for expected in ("s3", "local", "b2", "google cloud storage", "sftp", "webdav"):
        assert expected in names, f"{expected} missing from /api/providers"

    status, s3 = orch.json("GET", "/api/providers/s3")
    assert status == 200
    option_names = {o["name"] for o in s3["options"]}
    assert {"endpoint", "access_key_id", "secret_access_key", "provider"} <= option_names
    assert s3["bucket_based"] is True
    # Options flagged as passwords are the ones we must never return values for.
    assert any(o["password"] for o in s3["options"] if o["name"] == "secret_access_key")
    # An unknown backend is a 404, not a 500.
    assert orch.json("GET", "/api/providers/not-a-backend")[0] == 404


def test_operation_catalogue_exposes_flags_per_operation(orch: Orch):
    status, data = orch.json("GET", "/api/operations")
    assert status == 200
    names = {o["name"] for o in data["operations"]}
    assert {"copy", "sync", "move", "delete", "purge", "check", "size"} <= names

    status, copy_flags = orch.json("GET", "/api/operations?operation=copy")
    assert status == 200
    supported = {f["name"] for f in copy_flags["flags"] if f["supported"]}
    assert {"dry_run", "exclude", "transfers", "checksums"} <= supported
    # rclone's check has no --dry-run; the UI must not offer it.
    status, check_flags = orch.json("GET", "/api/operations?operation=check")
    check_supported = {f["name"]: f["supported"] for f in check_flags["flags"]}
    assert check_supported["dry_run"] is False
    assert check_supported["one_way"] is True
    assert orch.json("GET", "/api/operations?operation=nope")[0] == 404


def test_remote_form_pages_render_for_every_backend(orch: Orch):
    status, body = orch.html("/remotes/new")
    assert status == 200
    for backend in ("s3", "b2", "sftp", "google cloud storage"):
        assert f'value="{backend}"' in body, f"{backend} missing from the backend picker"
    status, _ = orch.html("/transfers")
    assert status == 200
    for op in ("copy", "sync", "move", "delete", "check"):
        status, body = orch.html(f"/transfers/form?operation={op}")
        assert status == 200 and f'value="{op}"' in body


def test_console_copy_runs_and_reports_structured_progress(orch: Orch):
    source = orch.src / "console"
    orch.write(source / "a.bin", FILE5 * 400)
    orch.write(source / "b.bin", FILE3_V1 * 400)

    # A dry run must not write anything.
    run_id = orch.run_operation("copy", "src:console", "dst:console-dry", {"dry_run": True})
    assert orch.wait_run(run_id) == "success"
    assert not (orch.dst / "console-dry").exists(), "dry run wrote data"

    # The real run copies and reports percent/bytes/objects from rclone's stats.
    run_id = orch.run_operation("copy", "src:console", "dst:console", {"transfers": 4})
    assert orch.wait_run(run_id) == "success"
    progress = orch.progress(run_id)["progress"]
    assert progress["percent"] == 100.0
    assert progress["bytes"] == (len(FILE5) + len(FILE3_V1)) * 400
    assert progress["transfers"] == 2 and progress["total_transfers"] == 2
    assert progress["errors"] == 0
    assert sorted(orch.files_under(orch.dst / "console")) == ["a.bin", "b.bin"]

    status, run = orch.json("GET", f"/api/runs?limit=5")
    assert status == 200
    recorded = next(r for r in run["runs"] if r["run_id"] == run_id)
    assert recorded["operation"] == "copy" and recorded["files_transferred"] == 2


def test_console_sync_archives_overwritten_files(orch: Orch):
    source = orch.src / "synced"
    orch.write(source / "f.txt", FILE1_V1)
    run_id = orch.run_operation("sync", "src:synced", "dst:synced/latest", {"_confirmed": True})
    assert orch.wait_run(run_id) == "success"
    before = orch.files_under(orch.dst / "synced" / "latest")

    orch.write(source / "f.txt", FILE1_V2)  # changed
    (source / "g.txt").unlink(missing_ok=True)
    orch.write(source / "g.txt", FILE4)
    run_id = orch.run_operation(
        "sync",
        "src:synced",
        "dst:synced/latest",
        {"backup_dir": "dst:synced/archive/2020-01-01_00-00-00", "_confirmed": True},
    )
    assert orch.wait_run(run_id) == "success"
    assert orch.files_under(orch.dst / "synced" / "latest") != before
    # --backup-dir kept the old version instead of destroying it.
    archived = orch.files_under(orch.dst / "synced" / "archive" / "2020-01-01_00-00-00")
    assert archived.get("f.txt") == FILE1_V1


def test_console_delete_requires_confirmation_then_honours_a_dry_run(orch: Orch):
    orch.write(orch.src / "trash" / "x.txt", FILE5)
    run_id = orch.run_operation("copy", "src:trash", "dst:trash")
    assert orch.wait_run(run_id) == "success"
    assert (orch.dst / "trash" / "x.txt").exists()

    # Unconfirmed destructive operation -> 400 and nothing deleted.
    status, data = orch.json("POST", "/api/transfers/run",
                             {"operation": "delete", "source": "dst:trash"})
    assert status == 400 and "dry run" in str(data).lower(), data
    assert (orch.dst / "trash" / "x.txt").exists()

    # Confirmed deletion really deletes.
    run_id = orch.run_operation("delete", "dst:trash", "", {"_confirmed": True})
    assert orch.wait_run(run_id) == "success"
    assert not (orch.dst / "trash" / "x.txt").exists()


def test_console_rejects_unknown_flags_operations_and_remotes(orch: Orch):
    cases = [
        ({"operation": "rm-rf", "source": "src:"}, "unknown operation"),
        ({"operation": "copy", "source": "nosuchremote:x", "destination": "dst:x"}, "does not exist"),
        ({"operation": "copy", "source": "src:", "destination": "dst:x",
          "flags": {"evil": 1}}, "unknown flag"),
        ({"operation": "check", "source": "src:", "destination": "dst:x",
          "flags": {"dry_run": True}}, "not supported"),
    ]
    for payload, expected in cases:
        status, data = orch.json("POST", "/api/transfers/run", payload)
        assert status == 400, f"{payload} -> {status} {data}"
        assert expected in str(data).lower(), (payload, data)


def test_console_check_reports_missing_files(orch: Orch):
    orch.write(orch.src / "verify" / "keep.txt", FILE1_V1)
    orch.write(orch.src / "verify" / "only-src.txt", FILE2)
    run_id = orch.run_operation("copy", "src:verify", "dst:verify")
    assert orch.wait_run(run_id) == "success"
    (orch.dst / "verify" / "only-src.txt").unlink()

    run_id = orch.run_operation("check", "src:verify", "dst:verify",
                              {"missing_on_dst": "missing-dst.txt"})
    # rclone exits non-zero when it finds differences, which is exactly what a
    # check is for: the run is "failed" because the check did not pass.
    assert orch.wait_run(run_id) == "failed"
    progress = orch.progress(run_id)["progress"]
    # rclone check prints a text summary (it ignores --use-json-log); the app
    # parses it so the verify result is visible instead of just "failed".
    assert progress["matched"] == 1
    assert progress["missing"] == 1
    assert progress["differences"] == 1
    assert progress["percent"] == 100.0

    # Restore the missing file and the check passes.
    orch.write(orch.dst / "verify" / "only-src.txt", FILE2)
    run_id = orch.run_operation("check", "src:verify", "dst:verify", {"missing_on_dst": "missing-dst.txt"})
    assert orch.wait_run(run_id) == "success"


def test_browse_api_lists_any_remote(orch: Orch):
    orch.write(orch.src / "browse" / "sub" / "deep.txt", FILE1_V1)
    status, data = orch.json("GET", "/api/browse?remote=src")
    assert status == 200
    assert any(i["name"] == "browse" and i["type"] == "dir" for i in data["items"])
    status, data = orch.json("GET", "/api/browse?remote=src&path=browse")
    assert status == 200 and data["items"][0]["name"] == "sub"
    assert orch.json("GET", "/api/browse?remote=")[0] == 400  # empty remote


def test_about_reports_quota_for_local(orch: Orch):
    status, data = orch.json("GET", "/api/remotes/src/about")
    assert status == 200 and data["supported"] is True
    assert data["about"]["total"] > 0
    assert 0 <= data["about"]["used_pct"] <= 100


def test_non_s3_remote_options_round_trip_and_stay_secret(orch: Orch):
    """A non-S3 backend is configured, listed and never leaks its secret."""
    status, _ = orch.json(
        "POST",
        "/api/remotes",
        {
            "name": "b2test",
            "type": "b2",
            "endpoint": "https://api.backblazeb2.com",
            "access_key": "unused",
            "secret_key": "unused",
            "options": {"account": "acct-123", "key": "SUPER-SECRET-KEY"},
        },
    )
    assert status == 201
    try:
        status, listing = orch.json("GET", "/api/remotes")
        remote = next(r for r in listing["remotes"] if r["name"] == "b2test")
        assert remote["type"] == "b2"
        options = {o["name"]: o for o in remote["options"]}
        assert options["account"]["value"] == "acct-123"
        assert options["key"]["value"] is None and options["key"]["secret"] is True
        assert options["key"]["set"] is True
        # Nothing secret anywhere in the JSON response.
        assert "SUPER-SECRET-KEY" not in str(listing)
        # An option that the backend does not have is rejected.
        status, data = orch.json(
            "PUT", f"/api/remotes/{remote['id']}", {"options": {"not_an_option": "1"}}
        )
        assert status == 400
    finally:
        assert orch.json("DELETE", f"/api/remotes/{remote['id']}")[0] == 204


def test_settings_still_round_trip(orch: Orch):
    status, data = orch.json("GET", "/api/settings")
    assert status == 200 and "retain_days" in data
    assert orch.json("PUT", "/api/settings", {"retain_days": 21})[0] == 200
    status, data = orch.json("GET", "/api/settings")
    assert str(data["retain_days"]["value"]) == "21"



def test_api_can_read_and_update_a_job(orch: Orch):
    """The JSON API could only create/delete jobs before."""
    status, created = orch.json(
        "POST",
        "/api/jobs",
        {
            "name": "api-edit",
            "source_remote": "src",
            "source_path": "folder",
            "dest_remote": "dst",
            "dest_bucket": "edited",
            "schedule_cron": "0 4 * * *",
        },
    )
    assert status == 201, created
    job_id = created["id"]

    status, fetched = orch.json("GET", f"/api/jobs/{job_id}")
    assert status == 200 and fetched["name"] == "api-edit"
    assert fetched["schedule_cron"] == "0 4 * * *"

    status, updated = orch.json(
        "PUT",
        f"/api/jobs/{job_id}",
        {
            "name": "api-edited",
            "source_remote": "src",
            "source_path": "folder",
            "dest_remote": "dst",
            "dest_bucket": "edited2",
            "schedule_cron": "0 5 * * *",
            "enabled": False,
        },
    )
    assert status == 200, updated
    assert updated["name"] == "api-edited"
    assert updated["dest_bucket"] == "edited2"
    assert updated["schedule_cron"] == "0 5 * * *"
    assert updated["enabled"] is False

    # Guards still apply on update.
    assert orch.json("PUT", f"/api/jobs/{job_id}", {
        "name": "api-edited", "source_remote": "src", "dest_remote": "src"})[0] == 400
    assert orch.json("PUT", f"/api/jobs/{job_id}", {
        "name": "other", "source_remote": "src", "dest_remote": "dst",
        "schedule_cron": "99 * * * *"})[0] == 400
    assert orch.json("GET", "/api/jobs/999999")[0] == 404
    assert orch.json("DELETE", f"/api/jobs/{job_id}")[0] == 204


def test_two_mutating_operations_cannot_share_a_target(orch: Orch):
    """A second sync/delete on the same path is refused, not run in parallel."""
    orch.write(orch.src / "lock" / "a.bin", FILE5 * 2000)
    # A long copy into the destination claims `dst:locked`.
    first = orch.run_operation(
        "sync", "src:lock", "dst:locked", {"_confirmed": True, "transfers": 1}
    )
    # A delete on the same destination while it runs must be rejected.
    status, data = orch.json(
        "POST",
        "/api/transfers/run",
        {"operation": "delete", "source": "dst:locked", "flags": {"_confirmed": True}},
    )
    assert status == 400, data
    assert "busy" in str(data).lower(), data
    assert orch.wait_run(first) == "success"
    # Once it finishes the target is free again.
    run_id = orch.run_operation(
        "delete", "dst:locked", "", {"_confirmed": True}
    )
    assert orch.wait_run(run_id) == "success"


def test_copy_does_not_block_on_a_busy_target(orch: Orch):
    """Read-only/additive operations are not guarded."""
    orch.write(orch.src / "share" / "a.bin", FILE2)
    first = orch.run_operation("copy", "src:share", "dst:share")
    second = orch.run_operation("copy", "src:share", "dst:share-2")
    assert orch.wait_run(first) == "success"
    assert orch.wait_run(second) == "success"


def test_restore_completion_is_audited(orch: Orch):
    status, runs = orch.json("GET", "/api/runs?kind=restore&limit=5")
    assert status == 200 and runs["runs"], "no restore runs recorded"
    statuses = {r["status"] for r in runs["runs"]}
    assert "success" in statuses
    assert orch.json("GET", "/api/history?limit=200")[0] == 200


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
