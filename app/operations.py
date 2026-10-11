"""rclone operation catalogue and flag registry.

The Transfer Console can drive any rclone operation this module describes. Two
rules keep that safe and maintainable:

* **Allow-listed commands only.** `OPERATIONS` is the complete set of
  operations the UI can start. There is no free-form command string, and rclone
  is always spawned with `create_subprocess_exec` (never a shell).
* **Allow-listed, typed flags.** `FLAGS` maps a form field to one rclone flag,
  with its type, validation bounds and the exact operations that accept it —
  because rclone's support is per command (``check`` has no ``--dry-run``,
  ``purge`` has no ``--include``, ...). Anything not in the registry cannot be
  passed, so no request body can smuggle a flag.
"""

from dataclasses import dataclass


class FlagError(ValueError):
    """Raised for an unknown flag or an invalid value (surfaces as HTTP 400)."""


@dataclass(frozen=True)
class OperationSpec:
    """One rclone operation the UI can run."""

    name: str  # the rclone subcommand
    label: str
    category: str
    description: str
    needs_source: bool = True
    needs_destination: bool = True
    destructive: bool = False  # removes or overwrites data at the destination
    dry_run: bool = True  # supports --dry-run
    backup_dir: bool = False  # supports --backup-dir
    transfer: bool = True  # moves data (vs. read-only inspection)
    confirm: bool = False  # require an explicit confirmation in the UI
    advanced: bool = False  # hidden behind "advanced" in the picker
    exposed: bool = True  # shown in the UI at all
    note: str = ""

    def summary(self) -> dict:
        return {
            "name": self.name,
            "label": self.label,
            "category": self.category,
            "description": self.description,
            "needs_source": self.needs_source,
            "needs_destination": self.needs_destination,
            "destructive": self.destructive,
            "dry_run": self.dry_run,
            "backup_dir": self.backup_dir,
            "transfer": self.transfer,
            "confirm": self.confirm,
            "advanced": self.advanced,
            "note": self.note,
        }


# Operations that copy data between two paths.
_COPY_OPS = ("copy", "sync", "copyto", "move", "moveto", "bisync")
# Operations that work on a single path (listing/filtering applies).
_PATH_OPS = _COPY_OPS + ("delete", "size", "check")

_OPERATIONS = [
    # ---------------------------------------------------- copy & sync
    OperationSpec(
        "copy",
        "Copy",
        "Copy & sync",
        "Copy new and changed files to the destination, without deleting "
        "anything there. Safe to repeat.",
    ),
    OperationSpec(
        "sync",
        "Sync",
        "Copy & sync",
        "Make the destination identical to the source: copy new/changed files "
        "and DELETE everything at the destination that is not in the source.",
        destructive=True,
        backup_dir=True,
        confirm=True,
    ),
    OperationSpec(
        "copyto",
        "Copy to (single object)",
        "Copy & sync",
        "Copy one file or folder to an exact destination name, ignoring the "
        "source file name.",
        backup_dir=True,
        advanced=True,
    ),
    # ---------------------------------------------------- move & delete
    OperationSpec(
        "move",
        "Move",
        "Move & delete",
        "Copy new/changed files to the destination and delete the source files "
        "that were copied. Irreversible for the source.",
        destructive=True,
        backup_dir=True,
        confirm=True,
    ),
    OperationSpec(
        "moveto",
        "Move to (single object)",
        "Move & delete",
        "Move one file or folder to an exact destination name.",
        destructive=True,
        backup_dir=True,
        confirm=True,
        advanced=True,
    ),
    OperationSpec(
        "delete",
        "Delete files",
        "Move & delete",
        "Delete the files found at the source. Folders are kept. Irreversible.",
        needs_destination=False,
        destructive=True,
        confirm=True,
    ),
    OperationSpec(
        "purge",
        "Purge folder",
        "Move & delete",
        "Delete a folder and everything inside it. Irreversible.",
        needs_destination=False,
        destructive=True,
        confirm=True,
        advanced=True,
    ),
    OperationSpec(
        "cleanup",
        "Clean up empty folders",
        "Move & delete",
        "Remove empty folders under the source path.",
        needs_destination=False,
        destructive=True,
        advanced=True,
    ),
    OperationSpec(
        "bisync",
        "Two-way sync",
        "Move & delete",
        "rclone bisync: propagate changes in both directions. The first run "
        "must be a --resync.",
        destructive=True,
        backup_dir=True,
        confirm=True,
        advanced=True,
        note="Requires a first run with --resync; bisync keeps its own state.",
    ),
    # ---------------------------------------------------- verify
    OperationSpec(
        "check",
        "Verify (check)",
        "Verify",
        "Compare source and destination and report missing or different "
        "files. Nothing is transferred.",
        transfer=False,
    ),
    OperationSpec(
        "cryptcheck",
        "Verify encrypted remote",
        "Verify",
        "Check that an encrypted (crypt) remote really decrypts to the right "
        "data.",
        needs_destination=False,
        transfer=False,
        advanced=True,
    ),
    # ---------------------------------------------------- maintenance
    OperationSpec(
        "dedupe",
        "Find duplicates",
        "Maintenance",
        "Report duplicate files inside the source. Always runs as a dry run "
        "so it can never delete anything.",
        needs_destination=False,
        destructive=True,
        advanced=True,
        note="Runs in dry-run mode only; review the report before deleting.",
    ),
    # ---------------------------------------------------- inspect
    OperationSpec(
        "size",
        "Measure size",
        "Inspect",
        "Report the total size and object count of a path.",
        needs_destination=False,
        transfer=False,
        advanced=True,
    ),
]

OPERATIONS = {op.name: op for op in _OPERATIONS}


def get_operation(name: str):
    """OperationSpec by rclone subcommand, or None."""
    return OPERATIONS.get((name or "").strip())


def list_operations(exposed_only: bool = True) -> list:
    """All operations, in catalogue order."""
    return [op.summary() for op in _OPERATIONS if op.exposed or not exposed_only]
@dataclass(frozen=True)
class FlagSpec:
    """One allow-listed rclone flag the UI may set."""

    name: str  # field name in the API/JSON payload
    flag: str  # rclone flag, e.g. "--max-age"
    label: str
    type: str  # bool | int | string | list | enum
    ops: tuple = ()  # operations that accept it ("" = all exposed ones)
    group: str = "General"
    help: str = ""
    default: str = ""
    choices: tuple = ()
    minimum: int = 0
    maximum: int = 0
    placeholder: str = ""
    required: bool = False  # must be set for the operation to make sense
    force: str = ""  # always pass this value (e.g. dedupe is always a dry run)

    def supports(self, operation: str) -> bool:
        return not self.ops or operation in self.ops

    def to_dict(self, operation: str = "") -> dict:
        data = {
            "name": self.name,
            "flag": self.flag,
            "label": self.label,
            "type": self.type,
            "group": self.group,
            "help": self.help,
            "default": self.default,
            "choices": list(self.choices),
            "placeholder": self.placeholder,
            "required": self.required,
        }
        if operation:
            data["supported"] = self.supports(operation)
        return data


@dataclass(frozen=True)
class FlagSpec:
    """One allow-listed rclone flag the UI may set."""

    name: str  # field name in the API/JSON payload
    flag: str  # rclone flag, e.g. "--max-age"
    label: str
    type: str  # bool | int | string | list | enum
    ops: tuple = ()  # operations that accept it ("" = all exposed ones)
    group: str = "General"
    help: str = ""
    default: str = ""
    choices: tuple = ()
    minimum: int = 0
    maximum: int = 0
    placeholder: str = ""
    required: bool = False  # must be set for the operation to make sense
    force: str = ""  # always passed (e.g. dedupe always runs as a dry run)

    def supports(self, operation: str) -> bool:
        return not self.ops or operation in self.ops

    def to_dict(self, operation: str = "") -> dict:
        data = {
            "name": self.name,
            "flag": self.flag,
            "label": self.label,
            "type": self.type,
            "group": self.group,
            "help": self.help,
            "default": self.default,
            "choices": list(self.choices),
            "placeholder": self.placeholder,
            "required": self.required,
        }
        if operation:
            data["supported"] = self.supports(operation)
        return data


_FLAGS = [
    # ------------------------------------------------ safety
    FlagSpec(
        "dry_run",
        "--dry-run",
        "Dry run (no changes)",
        "bool",
        _COPY_OPS + ("delete", "purge", "dedupe"),
        "Safety",
        "Show what would happen without changing anything. Start here.",
    ),
    FlagSpec(
        "backup_dir",
        "--backup-dir",
        "Backup changed/overwritten files into",
        "string",
        _COPY_OPS,
        "Safety",
        "Files that would be overwritten or deleted are first moved to this "
        "path, e.g. remote:bucket/archive/2026-01-01_00-00-00",
        placeholder="remote:bucket/prefix",
    ),
    FlagSpec(
        "max_delete",
        "--max-delete",
        "Stop after N deletions",
        "int",
        ("sync", "delete", "bisync"),
        "Safety",
        "Abort the run if it would delete more than this many files. Use it "
        "to guard against a wrong filter.",
        minimum=0,
    ),
    FlagSpec(
        "max_delete_size",
        "--max-delete-size",
        "Stop after deleting more than",
        "string",
        ("sync", "delete", "bisync"),
        "Safety",
        "Abort the run if it would delete more than this size, e.g. 10GiB.",
        placeholder="10GiB",
    ),
    FlagSpec(
        "ignore_errors",
        "--ignore-errors",
        "Continue after errors",
        "bool",
        _COPY_OPS + ("delete",),
        "Safety",
        "Carry on instead of aborting on the first error. Dangerous: it can "
        "hide data loss.",
    ),
    FlagSpec(
        "max_transfer",
        "--max-transfer",
        "Transfer at most",
        "string",
        _COPY_OPS,
        "Safety",
        "Stop after transferring this much data, e.g. 500GiB. Re-run later "
        "to continue where it stopped.",
        placeholder="500GiB",
    ),
    FlagSpec(
        "max_duration",
        "--max-duration",
        "Run for at most",
        "string",
        _COPY_OPS,
        "Safety",
        "Stop the run after this duration, e.g. 30m.",
        placeholder="30m",
    ),
    # ------------------------------------------------ filtering
    FlagSpec(
        "include",
        "--include",
        "Only these files",
        "list",
        _PATH_OPS,
        "Filtering",
        "rclone filter patterns to include, e.g. *.jpg or docs/**.",
        placeholder="*.jpg, docs/**",
    ),
    FlagSpec(
        "exclude",
        "--exclude",
        "Never these files",
        "list",
        _PATH_OPS,
        "Filtering",
        "rclone filter patterns to exclude, e.g. node_modules/**.",
        placeholder="node_modules/**, *.tmp",
    ),
    FlagSpec(
        "exclude_if_present",
        "--exclude-if-present",
        "Skip folders containing",
        "list",
        _COPY_OPS,
        "Filtering",
        "Skip directories containing a file with this name, e.g. .nobackup.",
        placeholder=".nobackup",
    ),
    FlagSpec(
        "min_age",
        "--min-age",
        "Only files older than",
        "string",
        _PATH_OPS,
        "Filtering",
        "Ignore files modified within this duration, e.g. 24h or 7d.",
        placeholder="24h",
    ),
    FlagSpec(
        "max_age",
        "--max-age",
        "Only files newer than",
        "string",
        _PATH_OPS,
        "Filtering",
        "Ignore files older than this duration, e.g. 30d.",
        placeholder="30d",
    ),
# ------------------------------------------------ transfer behaviour
    FlagSpec(
        "ignore_existing",
        "--ignore-existing",
        "Skip files that already exist",
        "bool",
        _COPY_OPS,
        "Transfer",
        "Skip any file already present at the destination. Useful for seeding "
        "a partially filled destination.",
    ),
    FlagSpec(
        "update",
        "--update",
        "Skip files newer at the destination",
        "bool",
        _COPY_OPS,
        "Transfer",
        "Only transfer files that are newer than the destination copy.",
    ),
    FlagSpec(
        "ignore_size",
        "--ignore-size",
        "Skip the size check",
        "bool",
        _COPY_OPS,
        "Transfer",
        "Consider files equal when their modification times match, ignoring "
        "size. Faster, less safe.",
    ),
    FlagSpec(
        "size_only",
        "--size-only",
        "Skip unless the size differs",
        "bool",
        _PATH_OPS,
        "Transfer",
        "Skip files whose size matches the destination copy.",
    ),
    FlagSpec(
        "checksums",
        "--checksum",
        "Compare by checksum",
        "bool",
        _PATH_OPS,
        "Transfer",
        "Compare checksums instead of size+modtime. Detects same-size "
        "modifications; costs an extra read on both sides.",
    ),
    FlagSpec(
        "track_renames",
        "--track-renames",
        "Track renames",
        "bool",
        ("sync", "move", "bisync"),
        "Transfer",
        "Detect renames and move files server-side instead of re-uploading.",
    ),
    FlagSpec(
        "cutoff_mode",
        "--cutoff-mode",
        "When a limit is reached",
        "enum",
        _COPY_OPS,
        "Transfer",
        "HARD aborts, SOFT stops gracefully, CAUTIOUS also undoes the partial "
        "transfer.",
        choices=("HARD", "SOFT", "CAUTIOUS"),
        default="HARD",
    ),
    FlagSpec(
        "create_empty_src_dirs",
        "--create-empty-src-dirs",
        "Recreate empty folders",
        "bool",
        ("sync", "move", "bisync"),
        "Transfer",
        "Create empty directories at the destination too.",
    ),
    FlagSpec(
        "delete_empty_src_dirs",
        "--delete-empty-src-dirs",
        "Remove empty source folders",
        "bool",
        ("move",),
        "Transfer",
        "Delete empty directories in the source after a move.",
    ),
    FlagSpec(
        "no_traverse",
        "--no-traverse",
        "Do not list the destination",
        "bool",
        _COPY_OPS,
        "Transfer",
        "Check each destination file individually. Faster with many files on "
        "a slow-listing backend.",
    ),
    FlagSpec(
        "order_by",
        "--order-by",
        "Transfer order",
        "string",
        _COPY_OPS,
        "Transfer",
        "e.g. size,descending — transfers the largest files first.",
        placeholder="size,descending",
    ),
    FlagSpec(
        "suffix",
        "--suffix",
        "Keep old versions as",
        "string",
        ("sync", "move", "moveto", "bisync"),
        "Transfer",
        "Suffix appended to the previous file instead of overwriting it.",
        placeholder=".old",
    ),
    FlagSpec(
        "partial_suffix",
        "--partial-suffix",
        "Incomplete file suffix",
        "string",
        _COPY_OPS,
        "Transfer",
        "Suffix for unfinished transfers so partial files are never mistaken "
        "for complete ones.",
        placeholder=".partial",
    ),
    FlagSpec(
        "links",
        "--links",
        "Transfer symlinks as symlinks",
        "bool",
        _COPY_OPS,
        "Transfer",
        "Recreate symbolic links instead of following them.",
    ),
    FlagSpec(
        "copy_links",
        "--copy-links",
        "Follow symlinks",
        "bool",
        _COPY_OPS,
        "Transfer",
        "Follow symbolic links and copy the file they point at.",
    ),
    FlagSpec(
        "fast_list",
        "--fast-list",
        "Fast list",
        "bool",
        _PATH_OPS,
        "Transfer",
        "Use a recursive listing instead of many per-folder calls. Uses more "
        "memory, far fewer transactions.",
    ),
# ------------------------------------------------ performance
    FlagSpec(
        "transfers",
        "--transfers",
        "Parallel file transfers",
        "int",
        _PATH_OPS,
        "Performance",
        "Files copied in parallel. More uses more bandwidth, CPU and memory.",
        minimum=1,
        maximum=64,
    ),
    FlagSpec(
        "checkers",
        "--checkers",
        "Parallel checkers",
        "int",
        _PATH_OPS,
        "Performance",
        "Files compared in parallel.",
        minimum=1,
        maximum=64,
    ),
    FlagSpec(
        "multi_thread_streams",
        "--multi-thread-streams",
        "Parallel streams per download",
        "int",
        _COPY_OPS,
        "Performance",
        "Threads per download for multi-threaded transfers.",
        minimum=1,
        maximum=64,
    ),
    FlagSpec(
        "buffer_size",
        "--buffer-size",
        "In-memory buffer",
        "string",
        _COPY_OPS,
        "Performance",
        "Read/write buffer per transfer, e.g. 16Mi. Larger can help on slow "
        "links.",
        placeholder="16Mi",
    ),
    FlagSpec(
        "bwlimit",
        "--bwlimit",
        "Bandwidth limit",
        "string",
        (),  # accepted by every operation
        "Performance",
        "Cap total transfer speed, e.g. 50M, or a timetable such as "
        "08:00,512k 18:00,10M.",
        placeholder="50M",
    ),
    FlagSpec(
        "bwlimit_file",
        "--bwlimit-file",
        "Per-file bandwidth limit",
        "string",
        (),  # accepted by every operation
        "Performance",
        "Cap the speed of each single transfer.",
        placeholder="10M",
    ),
    FlagSpec(
        "retries",
        "--retries",
        "Retries per operation",
        "int",
        (),  # accepted by every operation
        "Performance",
        "How often to retry a failed operation. The app default is 1 (fail "
        "fast); raise it for flaky links.",
        minimum=0,
        maximum=100,
    ),
    FlagSpec(
        "low_level_retries",
        "--low-level-retries",
        "Low-level retries",
        "int",
        (),  # accepted by every operation
        "Performance",
        "Retries inside the backend itself.",
        minimum=0,
        maximum=100,
    ),
    FlagSpec(
        "contimeout",
        "--contimeout",
        "Connect timeout",
        "string",
        (),  # accepted by every operation
        "Performance",
        "How long to wait for a connection, e.g. 10s.",
        placeholder="10s",
    ),
    FlagSpec(
        "timeout",
        "--timeout",
        "IO timeout",
        "string",
        (),  # accepted by every operation
        "Performance",
        "Abort a transfer that stalls for longer than this, e.g. 5m.",
        placeholder="5m",
    ),
    # ------------------------------------------------ verify (check/bisync)
    FlagSpec(
        "one_way",
        "--one-way",
        "One-way check",
        "bool",
        ("check",),
        "Verify",
        "Only confirm that the source files exist at the destination, without "
        "comparing them in detail (much faster on large trees).",
    ),
    FlagSpec(
        "combined",
        "--combined",
        "Write combined report to",
        "string",
        ("check",),
        "Verify",
        "Also write a combined report file.",
        placeholder="report.txt",
    ),
    # NOTE: --missing-on-src/--missing-on-dst take a *file path* in rclone, not a
    # boolean; the report is written inside the container. The counts are also
    # parsed from the run log, so the UI shows them either way.
    FlagSpec(
        "missing_on_src",
        "--missing-on-src",
        "Write missing-at-source report to",
        "string",
        ("check",),
        "Verify",
        "Report the files that exist at the destination but not in the source "
        "to this file.",
        placeholder="missing-src.txt",
    ),
    FlagSpec(
        "missing_on_dst",
        "--missing-on-dst",
        "Write missing-at-destination report to",
        "string",
        ("check",),
        "Verify",
        "Report the files that exist in the source but not at the destination "
        "to this file.",
        placeholder="missing-dst.txt",
    ),
    FlagSpec(
        "download",
        "--download",
        "Re-download and compare",
        "bool",
        ("check",),
        "Verify",
        "Download both sides and compare the data. Very slow, fully certain.",
    ),
    FlagSpec(
        "resync",
        "--resync",
        "First bisync run",
        "bool",
        ("bisync",),
        "Verify",
        "Perform the initial bisync resynchronisation. Only needed once per "
        "path pair.",
    ),
    # ------------------------------------------------ inspect (size only)
    FlagSpec(
        "max_size",
        "--max-size",
        "Only files smaller than",
        "string",
        ("size",),
        "Inspect",
        "Ignore larger files when measuring.",
        placeholder="1GiB",
    ),
    FlagSpec(
        "min_size",
        "--min-size",
        "Only files larger than",
        "string",
        ("size",),
        "Inspect",
        "Ignore smaller files when measuring.",
        placeholder="1M",
    ),
]

FLAGS = {f.name: f for f in _FLAGS}


def flags_for_operation(operation: str) -> list:
    """Registry payload for one operation (marks unsupported flags)."""
    return [f.to_dict(operation) for f in _FLAGS]


def _validate_value(spec: FlagSpec, value):
    """Coerce + range-check one flag value. Raises FlagError."""
    if spec.type == "bool":
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in ("true", "1", "yes", "on"):
            return True
        if text in ("false", "0", "no", "off", ""):
            return False
        raise FlagError(f"{spec.name}: expected true or false, got '{value}'")

    if spec.type == "list":
        if isinstance(value, (list, tuple)):
            raw_items = [str(v).strip() for v in value]
        else:
            # Accept a single string with comma- or newline-separated patterns.
            raw_items = [
                p.strip() for chunk in str(value).split("\n") for p in chunk.split(",")
            ]
        parts = [p for p in raw_items if p]
        if not parts:
            return ""
        if len(parts) > 64:
            raise FlagError(f"{spec.name}: too many values (max 64)")
        for part in parts:
            if any(ch in part for ch in ("\n", "\r", "\x00")):
                raise FlagError(f"{spec.name}: newlines are not allowed")
            if len(part) > 1024:
                raise FlagError(f"{spec.name}: value is too long")
        return parts

    text = str(value).strip()
    if spec.type == "int":
        try:
            number = int(text)
        except ValueError as exc:
            raise FlagError(
                f"{spec.name}: expected a whole number, got '{value}'"
            ) from exc
        if spec.minimum and number < spec.minimum:
            raise FlagError(f"{spec.name}: must be at least {spec.minimum}")
        if spec.maximum and number > spec.maximum:
            raise FlagError(f"{spec.name}: must be at most {spec.maximum}")
        return number

    if spec.type == "enum":
        if not spec.choices or text not in spec.choices:
            allowed = ", ".join(spec.choices) or "(any)"
            raise FlagError(f"{spec.name}: must be one of {allowed}, got '{value}'")
        return text

    if not text:
        return ""
    # These values reach rclone through argv (never a shell), but a newline
    # would corrupt the generated log line and is never legitimate here.
    if any(ch in text for ch in ("\n", "\r", "\x00")):
        raise FlagError(f"{spec.name}: newlines are not allowed")
    if len(text) > 2048:
        raise FlagError(f"{spec.name}: value is too long")
    return text


def validate_flags(raw: dict, operation: str, require_confirmed: bool = False) -> dict:
    """Validate a submitted flag map for one operation.

    Unknown keys, flags the operation does not support and out-of-range values
    are all rejected, so exactly what the registry allows reaches rclone. Forced
    flags (dedupe's dry run) are always applied.
    """
    spec = get_operation(operation)
    if spec is None:
        raise FlagError(f"unknown operation '{operation}'")
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise FlagError("flags must be an object")

    # Control key, not an rclone flag: the UI sends it when the user ticked the
    # "I understand this changes data" box for a destructive operation.
    confirmed = bool(raw.get("_confirmed"))
    clean: dict = {}
    for key, value in raw.items():
        if key == "_confirmed":
            continue
        flag = FLAGS.get(key)
        if flag is None:
            raise FlagError(f"unknown flag '{key}'")
        if not flag.supports(operation):
            raise FlagError(f"'{key}' is not supported by '{operation}'")
        parsed = _validate_value(flag, value)
        if parsed == "" or parsed is None:
            continue
        if flag.type == "bool" and parsed is False:
            continue  # false is rclone's default; keep the argv readable
        clean[key] = parsed

    if operation == "dedupe":
        # `rclone dedupe` prompts interactively unless it is a dry run, which
        # would hang the worker forever: force it on.
        clean["dry_run"] = True

    if (
        spec.destructive
        and require_confirmed
        and not clean.get("dry_run")
        and not confirmed
    ):
        raise FlagError(
            f"'{operation}' changes or removes data: run it as a dry run first, "
            "or confirm the operation explicitly"
        )
    return clean


def flag_argv(operation: str, flags: dict) -> list:
    """Turn a validated flag map into rclone argv fragments."""
    out: list = []
    for key, spec in FLAGS.items():
        if key not in flags:
            continue
        value = flags[key]
        if spec.type == "bool":
            if value:
                out.append(spec.flag)
            continue
        for item in (value if isinstance(value, list) else [value]):
            out.extend([spec.flag, str(item)])
    return out


def build_argv(
    operation: str,
    source: str,
    destination: str = "",
    flags: dict | None = None,
    extra: list | None = None,
) -> list:
    """Assemble the full rclone argument vector for one operation.

    `extra` carries arguments the caller controls (the backup engine appends
    its computed --backup-dir there), already validated.
    """
    spec = get_operation(operation)
    if spec is None:
        raise FlagError(f"unknown operation '{operation}'")
    if spec.needs_source and not (source or "").strip():
        raise FlagError(f"'{operation}' requires a source")

    argv = [operation, source.strip()]
    if spec.needs_destination:
        if not (destination or "").strip():
            raise FlagError(f"'{operation}' requires a destination")
        argv.append(destination.strip())
    argv.extend(flag_argv(operation, flags or {}))
    if extra:
        argv.extend(extra)
    return argv
