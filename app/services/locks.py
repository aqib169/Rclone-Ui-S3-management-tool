"""Per-target concurrency guard.

Two rclone processes must never write the same destination at the same time:
a `sync` and a `delete` on one bucket will happily delete each other's files.
The scheduler cannot express that constraint, so it is enforced here — a run
claims the paths it will *modify* and a conflicting start is refused (409)
instead of corrupting data.

Read-only and purely additive operations (`copy`, `check`, `size`, ...) are not
guarded: several of them may run against the same path safely.
"""

# Operations that only ever add data or read it.
_NON_MUTATING = {"copy", "copyto", "check", "cryptcheck", "size", "dedupe"}

# target -> run_id of the run currently modifying it
_claims: dict[str, str] = {}


class TargetBusy(RuntimeError):
    """Another run is already modifying one of the requested paths."""

    def __init__(self, target: str, other_run: str):
        self.target = target
        self.other_run = other_run
        super().__init__(f"another operation is already running on {target}")


def mutating_targets(operation: str, source: str, destination: str) -> list[str]:
    """The paths an operation will modify (empty for read-only operations)."""
    if (operation or "").strip() in _NON_MUTATING:
        return []
    # A delete/purge/cleanup works on its single argument; everything else that
    # mutates data writes to the destination.
    target = destination or source
    return [target.strip().rstrip("/")] if target and target.strip() else []


def claim(run_id: str, targets: list[str]) -> None:
    """Reserve `targets` for `run_id`; raise TargetBusy on any conflict."""
    busy = [t for t in targets if _claims.get(t) and _claims[t] != run_id]
    if busy:
        raise TargetBusy(busy[0], _claims[busy[0]])
    for target in targets:
        _claims[target] = run_id


def release(run_id: str) -> None:
    """Drop every claim held by a finished run."""
    for target in [t for t, owner in _claims.items() if owner == run_id]:
        _claims.pop(target, None)


def active() -> dict:
    """Current claims (used by /api/runs and the UI to explain a 409)."""
    return dict(_claims)