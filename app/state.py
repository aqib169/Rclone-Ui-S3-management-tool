"""In-memory state for the single-process orchestrator."""

from typing import Any

# run_id -> {proc, job_name, kind, source, destination}
active_runs: dict[str, dict[str, Any]] = {}

# run_id -> {paths: [..], mode: "files"|"all"} for restore runs
restore_jobs: dict[str, dict[str, Any]] = {}

# run_id -> live progress dict (see app.progress). Written by the transfer
# runner as rclone appends JSON log lines; read by the SSE progress endpoint and
# the dashboard. Cleared when the run finishes.
run_progress: dict[str, dict[str, Any]] = {}


def set_progress(run_id: str, progress: dict) -> None:
    """Publish the latest progress snapshot for a run."""
    if progress:
        run_progress[run_id] = progress


def clear_progress(run_id: str) -> None:
    run_progress.pop(run_id, None)
