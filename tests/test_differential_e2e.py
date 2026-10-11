#!/usr/bin/env python3
"""End-to-end integration test for the differential backup/restore lifecycle.

Boots a real uvicorn instance of the orchestrator against a clean, isolated
SQLite data dir and validates the full lifecycle against LOCAL directory remotes:

  Scenario 1  Isolated setup (src/dst/rt remotes, clean DB)
  Scenario 2  Run 1 - baseline backup into dst:bucket/latest (no deltas)
  Scenario 3  Run 2 - modify/delete/add files -> archive delta with OLD versions
  Scenario 4  Run 3 - restore latest + historical point-in-time recovery
  Scenario 5  Run 4 - cron parsing + one real scheduled job fire
  Scenario 6  Run 5 - retention pruning of expired archive deltas

The app process and all temp directories are torn down at the end regardless
of pass/fail. Exit code is non-zero if any assertion fails.

Standalone run:
    .venv/bin/python tests/test_differential_e2e.py

The comprehensive pytest suite (backup/restore lifecycle, restores, retention,
cron) lives in ``tests/test_e2e_lifecycle.py``:
    .venv/bin/python -m pytest tests/test_e2e_lifecycle.py -v
"""

import base64
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
RCLONE_BIN = os.environ.get("RCLONE_BIN", "/usr/bin/rclone")
USERNAME = "admin"
PASSWORD = "12345678"

_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}$")

SERVER: subprocess.Popen | None = None
BASE_URL = ""
PASS = 0
FAIL = 0
FAILURES: list[str] = []


# ---------------------------------------------------------------- helpers
def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"    [PASS] {name}")
    else:
        FAIL += 1
        FAILURES.append(name)
        print(f"    [FAIL] {name} {detail}")


def _auth_header() -> str:
    token = base64.b64encode(f"{USERNAME}:{PASSWORD}".encode()).decode()
    return "Basic " + token


def _request(method: str, path: str, payload=None, timeout: int = 30):
    headers = {"Authorization": _auth_header()}
    body = None
    if payload is not None:
        body = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(BASE_URL + path, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


def api(method: str, path: str, payload=None, timeout: int = 30):
    status, body = _request(method, path, payload, timeout=timeout)
    data: object = None
    if body and body.strip():
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            data = body
    return status, data


def wait_for_run(run_id: str, timeout: int = 120) -> tuple[str, str]:
    """Poll /runs/{id}/status until terminal. Returns (status, detail)."""
    deadline = time.time() + timeout
    last = "pending"
    while time.time() < deadline:
        try:
            status, data = api("GET", f"/api/runs/{run_id}/status")
            if status == 200 and isinstance(data, dict):
                last = data.get("status", last)
                if last in ("success", "failed", "cancelled"):
                    detail = ""
                    _, logs = api("GET", f"/api/runs/{run_id}/logs", timeout=10)
                    if isinstance(logs, dict):
                        detail = " | ".join(
                            l.get("message", "") for l in (logs.get("lines") or [])[-3:]
                        )
                    return last, detail
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.5)
    return last, "timeout"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_server(data_dir: Path) -> None:
    global SERVER, BASE_URL
    port = free_port()
    BASE_URL = f"http://127.0.0.1:{port}"
    env = dict(os.environ)
    env.update(
        {
            "DATA_DIR": str(data_dir),
            "USERNAME": USERNAME,
            "PASSWORD": PASSWORD,
            "RCLONE_BIN": RCLONE_BIN,
            "RETAIN_DAYS": "2",
        }
    )
    SERVER = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(REPO),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.time() + 30
    while time.time() < deadline:
        if SERVER.poll() is not None:
            raise RuntimeError("orchestrator exited during startup")
        try:
            status, _ = _request("GET", "/health", timeout=2)
            if status == 200:
                return
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.5)
    raise RuntimeError("orchestrator did not become healthy")


def stop_server() -> None:
    global SERVER
    if SERVER is None:
        return
    SERVER.terminate()
    try:
        SERVER.wait(timeout=10)
    except subprocess.TimeoutExpired:
        SERVER.kill()
        SERVER.wait(timeout=10)
    SERVER = None


def write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def files_under(root: Path) -> dict[str, bytes]:
    root = Path(root)
    if not root.exists():
        return {}
    return {
        str(p.relative_to(root)): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def total_bytes(root: Path) -> int:
    root = Path(root)
    if not root.exists():
        return 0
    return sum(p.stat().st_size for p in root.rglob("*") if p.is_file())


# ---------------------------------------------------------------- scenarios
def scenario_setup(src: Path, dst: Path, rt: Path) -> int:
    print("\n=== Scenario 1: setup (isolated remotes + clean instance) ===")
    for name, path in (("src", src), ("dst", dst), ("rt", rt)):
        status, _ = api(
            "POST",
            "/api/remotes",
            {"name": name, "type": "local", "endpoint": str(path), "access_key": "x", "secret_key": "y"},
        )
        check(f"create remote '{name}'", status in (201, 409))

    status, data = api(
        "POST",
        "/api/jobs",
        {
            "name": "job1",
            "source_remote": "src",
            "source_bucket": "",
            "source_path": "folder",
            "dest_remote": "dst",
            "dest_bucket": "mybucket",
            "dest_path": "",
            "schedule_cron": "",
            "enabled": True,
        },
    )
    check("create job1 (src:folder -> dst:mybucket/latest)", status == 201, str(data))
    job_id = data.get("id") if isinstance(data, dict) else None
    check("job1 id returned", isinstance(job_id, int))
    return job_id


def scenario_run1(job_id: int, src: Path, dst: Path) -> None:
    print("\n=== Scenario 2: Run 1 - baseline backup (no deltas) ===")
    f1_v1 = b"file1-original-v1\n"
    f2 = b"file2\n"
    f3 = b"file3\n"
    write(src / "folder" / "file1.txt", f1_v1)
    write(src / "folder" / "file2.txt", f2)
    write(src / "folder" / "file3.txt", f3)

    status, data = api("POST", f"/api/jobs/{job_id}/run")
    run_id = data.get("run_id") if isinstance(data, dict) else None
    check("trigger manual run 1", status == 202 and run_id, str(data))

    status, detail = wait_for_run(run_id)
    check("run 1 status == success", status == "success", detail or status)

    latest = files_under(dst / "mybucket" / "latest")
    check(
        "latest/ holds all 3 files",
        latest.get("file1.txt") == f1_v1 and latest.get("file2.txt") == f2 and latest.get("file3.txt") == f3,
        str(sorted(latest)),
    )

    archive_dir = dst / "mybucket" / "archive"
    archive_empty = not archive_dir.exists() or not any(archive_dir.iterdir())
    check("archive/ is absent or empty after baseline", archive_empty)

    _, runs = api("GET", f"/api/runs?job_id={job_id}&kind=backup")
    latest_run = (runs.get("runs") or [{}])[0] if isinstance(runs, dict) else {}
    check("run 1 recorded as success in DB", latest_run.get("status") == "success", str(latest_run))


def scenario_run2(job_id: int, src: Path, dst: Path) -> str:
    print("\n=== Scenario 3: Run 2 - delta generation (modify/delete/add) ===")
    f1_v1 = b"file1-original-v1\n"
    f1_v2 = b"file1-modified-version-2\n"
    f2 = b"file2\n"
    f3 = b"file3\n"
    f4 = b"file4\n"

    write(src / "folder" / "file1.txt", f1_v2)   # modified
    (src / "folder" / "file2.txt").unlink()       # deleted
    write(src / "folder" / "file4.txt", f4)       # added

    status, data = api("POST", f"/api/jobs/{job_id}/run")
    run_id = data.get("run_id") if isinstance(data, dict) else None
    check("trigger manual run 2", status == 202 and run_id, str(data))

    status, detail = wait_for_run(run_id)
    check("run 2 status == success", status == "success", detail or status)

    latest = files_under(dst / "mybucket" / "latest")
    check(
        "latest/ has updated file1 + file3 + file4, no file2",
        latest.get("file1.txt") == f1_v2
        and latest.get("file3.txt") == f3
        and latest.get("file4.txt") == f4
        and "file2.txt" not in latest,
        str(sorted(latest)),
    )

    archive_dir = dst / "mybucket" / "archive"
    stamps = sorted(
        p.name for p in archive_dir.iterdir() if p.is_dir() and _TS_RE.match(p.name)
    )
    check("exactly one archive delta created", len(stamps) == 1, str(stamps))
    ts2 = stamps[0] if stamps else ""

    delta = files_under(archive_dir / ts2)
    check(
        "delta holds OLD file1 and DELETED file2",
        delta.get("file1.txt") == f1_v1 and delta.get("file2.txt") == f2,
        str(sorted(delta)),
    )
    check(
        "delta does NOT hold unchanged/new files",
        "file3.txt" not in delta and "file4.txt" not in delta,
        str(sorted(delta)),
    )

    expected = total_bytes(dst / "mybucket" / "latest") + total_bytes(archive_dir / ts2)
    actual = total_bytes(dst)
    check(
        "dst grew only by modified/deleted files (exact byte count)",
        actual == expected,
        f"actual={actual} expected={expected}",
    )
    two_full_copies = (
        len(f1_v1) + len(f2) + len(f3)
    ) + (
        len(f1_v2) + len(f3) + len(f4)
    )
    check(
        "storage is well under a full duplicate of all files",
        actual < two_full_copies,
        f"actual={actual} two_full_copies={two_full_copies}",
    )
    return ts2


def scenario_run3(dst: Path, rt: Path, ts2: str) -> None:
    print("\n=== Scenario 4: Run 3 - restore latest + point-in-time ===")
    f1_v1 = b"file1-original-v1\n"
    f1_v2 = b"file1-modified-version-2\n"
    f3 = b"file3\n"
    f4 = b"file4\n"

    # --- Test A: restore latest
    status, data = api(
        "POST",
        "/api/restore",
        {
            "source_remote": "dst",
            "source_bucket": "mybucket",
            "archive_date": "latest",
            "mode": "all",
            "target_remote": "rt",
            "target_bucket": "",
            "target_path": "restore-a",
        },
    )
    run_id = data.get("run_id") if isinstance(data, dict) else None
    check("trigger restore latest", status == 202 and run_id, str(data))
    status, detail = wait_for_run(run_id)
    check("restore latest success", status == "success", detail or status)

    restored = files_under(rt / "restore-a")
    check(
        "rt/ matches current src (file1 updated, file3, file4; no file2)",
        restored.get("file1.txt") == f1_v2
        and restored.get("file3.txt") == f3
        and restored.get("file4.txt") == f4
        and "file2.txt" not in restored,
        str(sorted(restored)),
    )

    # --- Test B: restore historical (reverse-overlay point-in-time)
    shutil.rmtree(rt, ignore_errors=True)
    rt.mkdir(parents=True, exist_ok=True)

    status, data = api(
        "POST",
        "/api/restore",
        {
            "source_remote": "dst",
            "source_bucket": "mybucket",
            "archive_date": ts2,
            "mode": "all",
            "target_remote": "rt",
            "target_bucket": "",
            "target_path": "restore-b",
        },
    )
    run_id = data.get("run_id") if isinstance(data, dict) else None
    check("trigger historical restore", status == 202 and run_id, str(data))
    status, detail = wait_for_run(run_id)
    check("historical restore success", status == "success", detail or status)

    recovered = files_under(rt / "restore-b")
    # >= semantics: restoring to a delta T also applies T's own delta (which
    # holds the pre-run versions), reverting run-2's changes and putting the
    # file deleted in run 2 back.
    check(
        "PITR to ts2 reverts run-2 changes (file1 original, file2 back)",
        recovered.get("file1.txt") == f1_v1
        and recovered.get("file2.txt") == b"file2\n"
        and recovered.get("file3.txt") == f3,
        str(sorted(recovered)),
    )
    # Reverse overlay uses `copy` (never deletes): files added by run 2 (file4)
    # remain in the target — documented additive behaviour.
    check(
        "PITR additive overlay leaves files added in run 2 (file4)",
        recovered.get("file4.txt") == f4,
        str(sorted(recovered)),
    )


def scenario_cron(job_id: int, dst: Path) -> None:
    print("\n=== Scenario 5: Run 4 - cron parsing + scheduled execution ===")
    os.environ["DATA_DIR"] = str(dst.parent / "data")
    os.environ["RCLONE_BIN"] = RCLONE_BIN
    from apscheduler.triggers.cron.expressions import AllExpression

    from app.scheduler import build_cron_trigger
    from app.schemas import validate_cron_schedule

    check("cron kept verbatim: '0 */3 * * *'", validate_cron_schedule("0 */3 * * *") == "0 */3 * * *")
    check("cron kept verbatim: '* * * * *'", validate_cron_schedule("* * * * *") == "* * * * *")
    check(
        "cron auto-sanitized: '* */3 * * *' -> '0 */3 * * *'",
        validate_cron_schedule("* */3 * * *") == "0 */3 * * *",
    )

    t = build_cron_trigger("0 */3 * * *")
    hour_step = getattr(t.fields[5].expressions[0], "step", None)
    minute = t.fields[6].expressions[0]
    check(
        "'0 */3 * * *' fires every 3 hours at minute 0",
        hour_step == 3 and type(minute) is not AllExpression and getattr(minute, "first", None) == 0,
        f"hour_step={hour_step} minute={type(minute).__name__} first={getattr(minute, 'first', None)}",
    )

    t = build_cron_trigger("* */3 * * *")
    hour_step = getattr(t.fields[5].expressions[0], "step", None)
    minute = t.fields[6].expressions[0]
    check(
        "'* */3 * * *' sanitized to fire at minute 0 (no more every-minute bug)",
        hour_step == 3 and type(minute) is not AllExpression and getattr(minute, "first", None) == 0,
        f"hour_step={hour_step} minute={type(minute).__name__} first={getattr(minute, 'first', None)}",
    )

    status, data = api(
        "POST",
        "/api/jobs",
        {
            "name": "job-cron",
            "source_remote": "src",
            "source_bucket": "",
            "source_path": "folder",
            "dest_remote": "dst",
            "dest_bucket": "mybucket2",
            "dest_path": "",
            "schedule_cron": "* * * * *",
            "enabled": True,
        },
    )
    cron_job_id = data.get("id") if isinstance(data, dict) else None
    check("create cron job", status == 201 and isinstance(cron_job_id, int), str(data))

    deadline = time.time() + 100
    fired = None
    polls = 0
    while time.time() < deadline:
        polls += 1
        st, runs = api("GET", f"/api/runs?job_id={cron_job_id}&kind=backup")
        for r in (runs.get("runs") or []) if isinstance(runs, dict) else []:
            if r.get("triggered_by") == "schedule":
                fired = r
                break
        if fired and fired.get("status") in ("success", "failed", "cancelled"):
            break
        time.sleep(2)
    print(f"    (cron polled {polls}x over ~{round(polls * 2)}s, fired={fired is not None})")
    check("cron job fired on its own", fired is not None)
    if fired is not None:
        check("cron run reached success", fired.get("status") == "success", fired.get("status"))
        check(
            "cron run synced into dst:mybucket2/latest",
            files_under(dst / "mybucket2" / "latest").get("file4.txt") == b"file4\n",
            str(sorted(files_under(dst / "mybucket2" / "latest"))),
        )


def scenario_retention(data_dir: Path, dst: Path, ts2: str) -> None:
    print("\n=== Scenario 6: Run 5 - retention pruning ===")
    write(dst / "mybucket" / "archive" / "2020-01-01_00-00-00" / "old.txt", b"old")
    check("mock expired archive created", (dst / "mybucket" / "archive" / "2020-01-01_00-00-00" / "old.txt").exists())

    latest_before = files_under(dst / "mybucket" / "latest")

    code = (
        "import asyncio, json, os\n"
        f"os.environ['DATA_DIR'] = {str(data_dir)!r}\n"
        f"os.environ['RCLONE_BIN'] = {RCLONE_BIN!r}\n"
        "from app.services.retention import prune_archives\n"
        "async def _m():\n"
        "    return await prune_archives('dst', 'mybucket', 2)\n"
        "print(json.dumps(asyncio.run(_m())))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(REPO),
        capture_output=True,
        text=True,
        timeout=120,
    )
    purged = []
    try:
        purged = json.loads(proc.stdout.strip())
    except json.JSONDecodeError:
        pass
    check("retention subprocess ran cleanly", proc.returncode == 0, proc.stderr.strip()[-400:])
    check("expired 2020-01-01 archive purged", "2020-01-01_00-00-00" in purged, str(purged))
    check(
        "recent delta (ts2) retained",
        (dst / "mybucket" / "archive" / ts2).is_dir(),
    )
    check(
        "latest/ untouched by pruning",
        files_under(dst / "mybucket" / "latest") == latest_before,
    )
    check(
        "old mock archive folder removed from disk",
        not (dst / "mybucket" / "archive" / "2020-01-01_00-00-00").exists(),
    )


def run_e2e() -> int:
    global PASS, FAIL
    PASS = 0
    FAIL = 0
    FAILURES.clear()
    work = tempfile.mkdtemp(prefix="garage-e2e-")
    work_root = Path(work)
    src = work_root / "src"
    dst = work_root / "dst"
    rt = work_root / "rt"
    data_dir = work_root / "data"
    try:
        start_server(data_dir)
        job_id = scenario_setup(src, dst, rt)
        if not isinstance(job_id, int):
            raise RuntimeError("could not create job1")
        scenario_run1(job_id, src, dst)
        ts2 = scenario_run2(job_id, src, dst)
        scenario_run3(dst, rt, ts2)
        scenario_cron(job_id, dst)
        scenario_retention(data_dir, dst, ts2)
    finally:
        stop_server()
        shutil.rmtree(work, ignore_errors=True)

    print("\n" + "=" * 60)
    print(f"RESULT: {PASS} passed, {FAIL} failed")
    if FAIL:
        print("Failed assertions:")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    return 0


def test_e2e_lifecycle() -> None:
    """Deprecated pytest entry point.

    The comprehensive pytest suite lives in ``tests/test_e2e_lifecycle.py``.
    This file is the standalone runner (see module docstring); running both in
    a single pytest process would fight over the module-level app settings, so
    the pytest wrapper is removed in favour of the dedicated suite.
    """
    raise SystemExit("use tests/test_e2e_lifecycle.py under pytest, or run this file directly")


if __name__ == "__main__":
    sys.exit(run_e2e())
