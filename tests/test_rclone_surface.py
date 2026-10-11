#!/usr/bin/env python3
"""Unit tests for the rclone surface: backends, options, operations, progress.

Covers the pieces that must never regress silently:

* the backend catalogue parsed from `rclone config providers`,
* remote option validation / encryption / INI rendering (injection-proof),
* the operation + flag registry (including that rclone itself accepts every
  flag/operation combination the registry claims),
* structured progress parsing for JSON *and* legacy text logs.

Run:
    .venv/bin/python -m pytest tests/test_rclone_surface.py -v
"""

import asyncio
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

# The app reads its data dir at import time; point it at a scratch dir so these
# tests never touch a real data/ directory.
SCRATCH_DATA_DIR = tempfile.mkdtemp(prefix="orch-unit-")
os.environ["DATA_DIR"] = SCRATCH_DATA_DIR
os.environ.setdefault("USERNAME", "admin")
os.environ.setdefault("PASSWORD", "unittest1234")
os.environ.setdefault("RCLONE_BIN", "/usr/bin/rclone")

from app.operations import (  # noqa: E402
    FLAGS,
    OPERATIONS,
    FlagError,
    build_argv,
    flags_for_operation,
    get_operation,
    list_operations,
    validate_flags,
)
from app.progress import (  # noqa: E402
    empty_progress,
    humanize_line,
    parse_log,
    progress_from_entry,
    read_progress,
)
from app.providers import (  # noqa: E402
    ProviderOption,
    _parse,
    bucket_based,
    get_provider,
    load_catalogue,
    reset_cache,
    secret_option_names,
)
from app.remote_config import (  # noqa: E402
    OptionError,
    allowed_option_names,
    encode_config,
    normalize_options,
    options_summary,
    parse_options,
    resolve_options,
)


@pytest.fixture(scope="module", autouse=True)
def catalogue():
    """Load the real backend catalogue once for the module."""
    providers = asyncio.run(load_catalogue(force_refresh=True))
    assert providers, "rclone returned no providers — is rclone installed?"
    return providers


@pytest.fixture
def s3_loaded(catalogue):
    """Guarantee the in-memory catalogue is populated for this test.

    Other tests here call `reset_cache()`, which empties the in-memory
    catalogue that `app.diagnostics` reads to validate provider names.
    """
    if not get_provider("s3"):
        asyncio.run(load_catalogue())
    yield get_provider("s3")


class _Row:
    """Minimal stand-in for a Remote row."""

    def __init__(self, **kw):
        self.type = "s3"
        self.endpoint = ""
        self.access_key = ""
        self.secret_key = ""
        self.region = "garage"
        self.provider = "Other"
        self.options = "{}"
        for key, value in kw.items():
            setattr(self, key, value)


# ------------------------------------------------------------------ providers
def test_catalogue_covers_every_backend(catalogue):
    names = {p.name for p in catalogue}
    for expected in ("s3", "local", "b2", "google cloud storage", "sftp", "webdav", "drive"):
        assert expected in names, f"{expected} missing from the catalogue"
    assert len(catalogue) > 20


def test_provider_detail_exposes_every_option():
    s3 = get_provider("s3")
    assert s3 is not None
    assert len(s3.options) > 50
    names = {o.name for o in s3.options}
    assert {"endpoint", "access_key_id", "secret_access_key", "region", "provider"} <= names
    provider_opt = next(o for o in s3.options if o.name == "provider")
    # The S3 provider enum (AWS, Cloudflare for R2, MinIO, Garage, ...) comes
    # straight from rclone and becomes a dropdown in the remote form.
    values = {e["value"] for e in provider_opt.examples}
    # Cloudflare R2 and MinIO are first-class choices; Garage itself is
    # configured as an S3-compatible endpoint (provider "Other"/Minio).
    assert {"AWS", "Cloudflare", "Minio"} <= values
    assert len(provider_opt.examples) > 30


def test_secret_and_bucket_metadata():
    assert "key" in secret_option_names("b2")
    assert "pass" in secret_option_names("sftp")
    assert bucket_based("s3") is True
    assert bucket_based("google cloud storage") is True
    assert bucket_based("local") is False
    # Marked secret even when rclone forgets to flag it.
    assert ProviderOption(name="secret_key").is_secret is True
    assert ProviderOption(name="endpoint").is_secret is False


def test_parse_normalises_and_sorts():
    raw = [
        {
            "Name": "zzz",
            "Description": "last",
            "Overview": {"Tier": "Tier 3", "Features": []},
            "Options": [{"Name": "host", "Type": "string", "Required": True}],
        },
        {
            "Name": "aaa",
            "Description": "first",
            "Overview": {"Tier": "Tier 1", "Features": ["BucketBased"]},
            "Options": [{"Name": "key", "IsPassword": True}],
        },
    ]
    providers = _parse(raw)
    assert [p.name for p in providers] == ["aaa", "zzz"]
    assert providers[0].bucket_based is True
    assert providers[0].options[0].is_secret is True


def test_catalogue_is_cached_on_disk():
    from app import config

    # Cached in the app's *current* data dir (another test module may have
    # re-pointed it), which is what a restart would read back.
    cache_file = Path(config.settings.data_dir) / ".rclone_providers.json"
    assert cache_file.exists(), "catalogue was not cached to disk"
    data = json.loads(cache_file.read_text())
    assert data["providers"] and data["generated_at"]
    # A fresh read (as after a restart) serves the catalogue without rclone.
    assert len(asyncio.run(load_catalogue())) > 20# ------------------------------------------------------------------ options
def test_options_are_validated_and_secrets_encrypted():
    clean = normalize_options({"account": "abc", "key": "TOP-SECRET"}, "b2")
    assert clean["account"] == "abc"
    assert clean["key"] != "TOP-SECRET", "secret option was stored in plaintext"
    row = _Row(type="b2", options=json.dumps(clean))
    assert resolve_options(row)["key"] == "TOP-SECRET"


def test_option_names_and_values_cannot_inject_ini():
    for bad_name in ("evil\n[section]", "bad name", "a=b", ""):
        with pytest.raises(OptionError):
            normalize_options({bad_name: "x"}, "s3")
    for bad_value in ("http://x\n[injected]\ntype = local", "a\r\nb", "a\x00b"):
        with pytest.raises(OptionError):
            normalize_options({"endpoint": bad_value}, "s3")
    with pytest.raises(OptionError):  # too long -> rejected, not truncated
        normalize_options({"endpoint": "x" * 5000}, "s3")


def test_unknown_option_for_known_backend_is_rejected():
    allowed = allowed_option_names("b2")
    assert "account" in allowed
    with pytest.raises(OptionError):
        normalize_options({"totally_made_up": "1"}, "b2", allowed)
    # An unknown backend (newer rclone than the cache) stays permissive.
    assert normalize_options({"whatever": "1"}, "not-a-real-backend")["whatever"] == "1"


def test_empty_values_are_not_stored():
    assert normalize_options({"region": "", "endpoint": "  "}, "s3") == {}


def test_rendered_config_is_valid_ini_and_accepted_by_rclone():
    clean = normalize_options({"account": "abc", "key": "s3cret"}, "b2")
    row = _Row(type="b2", options=json.dumps(clean))
    text = encode_config("myb2", "b2", resolve_options(row))
    assert text.startswith("[myb2]\ntype = b2\n")
    assert "account = abc" in text
    assert "key = s3cret" in text
    path = Path(tempfile.mkdtemp()) / "rclone.conf"
    path.write_text(text)
    proc = subprocess.run(
        [os.environ["RCLONE_BIN"], "--config", str(path), "config", "providers"],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr


def test_quoting_only_when_needed():
    assert encode_config("r", "s3", {"endpoint": "https://a.io"}) == (
        "[r]\ntype = s3\nendpoint = https://a.io\n"
    )
    assert 'endpoint = "a #b"' in encode_config("r", "s3", {"endpoint": "a #b"})


def test_legacy_s3_columns_are_merged_and_decrypted():
    from app.crypto import encrypt

    row = _Row(
        type="s3",
        endpoint="http://garage:3900",
        access_key=encrypt("AK"),
        secret_key=encrypt("SK"),
        provider="Garage",
        options=json.dumps({"endpoint": "http://override:3900"}),
    )
    resolved = resolve_options(row)
    assert resolved["endpoint"] == "http://override:3900"
    assert resolved["access_key_id"] == "AK"
    assert resolved["secret_access_key"] == "SK"
    assert resolved["provider"] == "Garage"


def test_local_remote_keeps_its_path_out_of_the_option_map():
    assert resolve_options(_Row(type="local", endpoint="/data/bucket")) == {}


def test_option_summary_never_returns_secret_values():
    clean = normalize_options({"account": "abc", "key": "TOPSECRET"}, "b2")
    row = _Row(type="b2", options=json.dumps(clean))
    summary = {o["name"]: o for o in options_summary(row)}
    assert summary["account"]["value"] == "abc"
    assert summary["key"]["value"] is None and summary["key"]["secret"] is True


def test_parse_options_tolerates_junk():
    assert parse_options(None) == {}
    assert parse_options("not json") == {}
    assert parse_options("[1,2]") == {}
    assert parse_options('{"a":"b"}') == {"a": "b"}


# ------------------------------------------------------------------ operations
def test_every_operation_is_described():
    names = {o["name"] for o in list_operations()}
    assert {"copy", "sync", "move", "delete", "purge", "check", "size"} <= names
    for spec in OPERATIONS.values():
        assert spec.label and spec.category and spec.description
    # Source-only operations must not demand a destination.
    assert get_operation("delete").needs_destination is False
    assert get_operation("sync").destructive is True
    assert get_operation("check").transfer is False


def test_argv_shape_per_operation():
    assert build_argv("copy", "s:b", "d:b") == ["copy", "s:b", "d:b"]
    assert build_argv("delete", "s:b") == ["delete", "s:b"]
    with pytest.raises(FlagError):
        build_argv("copy", "s:b")
    with pytest.raises(FlagError):
        build_argv("delete", "")


def test_flags_are_typed_and_scoped_to_the_operation():
    clean = validate_flags(
        {"exclude": ["*.tmp", "logs/**"], "max_delete": "25", "dry_run": "yes"}, "sync"
    )
    assert clean == {"exclude": ["*.tmp", "logs/**"], "max_delete": 25, "dry_run": True}
    # A comma-separated string is accepted for list flags (form input).
    assert validate_flags({"include": "*.jpg, docs/**"}, "copy")["include"] == [
        "*.jpg",
        "docs/**",
    ]
    # Out-of-range / bad enum / unsupported / unknown are all rejected.
    for bad, op in (
        ({"transfers": 999}, "copy"),
        ({"transfers": "abc"}, "copy"),
        ({"cutoff_mode": "SLOW"}, "copy"),
        ({"dry_run": True}, "check"),      # check has no --dry-run in rclone
        ({"include": ["a"]}, "purge"),     # purge has no --include
        ({"track_renames": True}, "copy"),
        ({"nope": 1}, "copy"),
        ({"backup_dir": "a\nb"}, "copy"),
    ):
        with pytest.raises(FlagError):
            validate_flags(bad, op)


def test_destructive_operations_need_a_dry_run_or_confirmation():
    with pytest.raises(FlagError):
        validate_flags({}, "delete", require_confirmed=True)
    # A dry run is always allowed.
    assert validate_flags({"dry_run": True}, "delete", require_confirmed=True)
    # Explicit confirmation unlocks a real run.
    assert validate_flags({"_confirmed": True}, "delete", require_confirmed=True) == {}
    # `_confirmed` never reaches rclone.
    assert "_confirmed" not in build_argv("delete", "s:b")


def test_dedupe_is_forced_to_dry_run():
    # rclone dedupe prompts interactively and would hang the worker forever.
    assert validate_flags({}, "dedupe")["dry_run"] is True
    assert "--dry-run" in build_argv("dedupe", "s:b", "", validate_flags({}, "dedupe"))


def test_flag_registry_is_serialisable_for_the_ui():
    for op in OPERATIONS:
        payload = flags_for_operation(op)
        assert payload
        for spec in payload:
            assert spec["name"] in FLAGS
        if op == "check":
            by_name = {s["name"]: s for s in payload}
            # rclone's check has neither of these...
            assert by_name["dry_run"]["supported"] is False
            assert by_name["backup_dir"]["supported"] is False
            # ...but it does have its own verification flags.
            assert by_name["one_way"]["supported"] is True


def test_registry_matches_what_rclone_accepts():
    """Cross-check every flag/operation pair against rclone itself.

    rclone's flag support is per command, so this is the only way to be sure the
    registry does not advertise something rclone would reject at runtime.
    """
    import re

    rclone = os.environ["RCLONE_BIN"]
    global_flags = set(
        re.findall(
            r"--[a-z0-9-]+",
            subprocess.run([rclone, "help", "flags"], capture_output=True, text=True).stdout,
        )
    )
    assert global_flags, "could not read rclone's global flags"
    mismatches = []
    for spec in FLAGS.values():
        for op in spec.ops or tuple(OPERATIONS):
            proc = subprocess.run([rclone, op, "--help"], capture_output=True, text=True)
            accepted = set(re.findall(r"--[a-z0-9-]+", proc.stdout + proc.stderr)) | global_flags
            if spec.flag not in accepted:
                mismatches.append((spec.name, spec.flag, op))
    assert not mismatches, f"registry claims flags rclone rejects: {mismatches}"


def test_registry_flag_arity_matches_rclone():
    """A boolean registry entry for a value-taking flag (or vice versa) would
    make rclone swallow the next argument — the arity must match too."""
    import re

    rclone = os.environ["RCLONE_BIN"]
    global_types = {}
    for line in subprocess.run(
        [rclone, "help", "flags"], capture_output=True, text=True
    ).stdout.splitlines():
        m = re.match(r"\s+(?:-\w, )?--([a-z0-9-]+)(?:[= ]([A-Za-z|]+))?\s{2,}\S", line)
        if m:
            global_types["--" + m.group(1)] = (m.group(2) or "").lower()
    assert global_types

    mismatches = []
    for spec in FLAGS.values():
        for op in spec.ops or tuple(OPERATIONS):
            proc = subprocess.run([rclone, op, "--help"], capture_output=True, text=True)
            declared = dict(global_types)
            for line in (proc.stdout + proc.stderr).splitlines():
                m = re.match(r"\s+(?:-\w, )?--([a-z0-9-]+)(?:[= ]([A-Za-z|]+))?\s{2,}\S", line)
                if m:
                    declared["--" + m.group(1)] = (m.group(2) or "").lower()
            kind = declared.get(spec.flag)
            if kind is None:
                mismatches.append((spec.name, op, "flag not accepted"))
                continue
            takes_value = kind not in ("bool", "count", "")
            if takes_value != (spec.type != "bool"):
                mismatches.append((spec.name, op, f"rclone={kind!r} registry={spec.type!r}"))
    assert not mismatches, f"flag arity mismatches: {mismatches}"


# ------------------------------------------------------------------ progress
_JSON_STATS = {
    "time": "2026-01-01T10:00:05.000Z",
    "level": "info",
    "msg": "Transferred: 1 MiB / 2 MiB, 50%",
    "stats": {
        "bytes": 1048576,
        "totalBytes": 2097152,
        "speed": 524288.0,
        "eta": 2.0,
        "transfers": 3,
        "totalTransfers": 8,
        "checks": 1,
        "totalChecks": 4,
        "deletes": 2,
        "deletedDirs": 1,
        "listed": 9,
        "renames": 0,
        "errors": 1,
        "elapsedTime": 2.5,
        "fatalError": False,
    },
}


def test_json_stats_become_structured_progress():
    progress = progress_from_entry(_JSON_STATS)
    assert progress["percent"] == 50.0
    assert progress["bytes"] == 1048576
    assert progress["total_bytes"] == 2097152
    assert progress["remaining_bytes"] == 1048576
    assert progress["transfers"] == 3 and progress["total_transfers"] == 8
    assert progress["speed"] == 524288.0
    assert progress["eta"] == 2.0
    assert progress["deletes"] == 2 and progress["deleted_dirs"] == 1
    assert progress["errors"] == 1
    # A non-stats entry yields no progress.
    assert progress_from_entry({"level": "info", "msg": "Copied"}) is None


def test_progress_is_never_above_100_percent():
    entry = {"stats": dict(_JSON_STATS["stats"], bytes=99999999, totalBytes=1000)}
    assert progress_from_entry(entry)["percent"] == 100.0


def test_log_scan_merges_counters():
    log = "\n".join(
        [
            json.dumps({"level": "info", "msg": "Copied (new)", "size": 5, "object": "a.txt"}),
            json.dumps({"level": "info", "msg": "Deleted", "object": "old.txt"}),
            json.dumps({"level": "error", "msg": "Failed to copy", "object": "bad.txt"}),
            json.dumps(_JSON_STATS),
        ]
    )
    result = parse_log(log)
    assert result["transfers"] == 3
    assert result["errors"] == 1
    # The closing stats snapshot is authoritative: it counted 2 deletes even
    # though only one "Deleted" line appears in the log.
    assert result["deletes"] == 2
    assert result["files"] == 3
    assert result["bytes"] >= 1048576


def test_legacy_text_log_still_parses():
    log = (
        "2026/01/01 10:00:00 INFO  : Transferred:   12 MiB / 12 MiB, 100%, 1.5 MiB/s, ETA -\n"
        "2026/01/01 10:00:00 INFO  : /src/f1.bin: Copied (new)\n"
        "2026/01/01 10:00:00 ERROR : /src/f3.bin: Failed to copy: AccessDenied\n"
    )
    result = parse_log(log)
    assert result["files"] == 1
    assert result["bytes"] == 12582912
    assert result["percent"] == 100.0
    assert result["errors"] == 1


def test_empty_log_is_safe():
    empty = parse_log("")
    assert empty["files"] == 0 and empty["percent"] == 0.0
    assert read_progress("/does/not/exist")["percent"] == 0.0
    assert empty_progress()["bytes"] == 0


def test_log_lines_keep_the_old_text_shape_for_the_ui():
    line = humanize_line(json.dumps(_JSON_STATS))
    assert "2026-01-01 10:00:05" in line and "INFO" in line
    copied = humanize_line(json.dumps({"time": "2026-01-01T10:00:06Z", "level": "info",
                                       "msg": "Copied (new)", "object": "a.txt"}))
    assert copied.endswith("a.txt")
    # Non-JSON (legacy) lines pass through unchanged.
    assert humanize_line("plain text line") == "plain text line"


# ------------------------------------------------------------------ endpoints
def test_endpoint_accepts_what_people_actually_type():
    """`10.10.21.79:3900` must work — a self-hosted node is typed without a scheme."""
    from app.schemas import _valid_endpoint

    assert _valid_endpoint("10.10.21.79:3900") == "http://10.10.21.79:3900"
    assert _valid_endpoint("garage.lan:3900") == "http://garage.lan:3900"
    assert _valid_endpoint("storage.example.com") == "http://storage.example.com"
    assert _valid_endpoint("http://10.0.0.1:3900") == "http://10.0.0.1:3900"
    assert _valid_endpoint("https://x.r2.cloudflarestorage.com") == (
        "https://x.r2.cloudflarestorage.com"
    )
    assert _valid_endpoint("/mnt/pics") == "/mnt/pics"  # local remote
    assert _valid_endpoint("[2001:db8::1]:9000") == "http://[2001:db8::1]:9000"
    for bad in ("", "   ", "http://", "has space:3900", "not a host!"):
        with pytest.raises(ValueError):
            _valid_endpoint(bad)


# ------------------------------------------------------------------ diagnostics
def test_r2_preflight_catches_the_common_mistakes():
    """Cloudflare R2 fails for a handful of well-known reasons; catch them
    before spending an HTTP round trip on a confusing error."""
    from app.diagnostics import preflight

    account = "https://0123456789abcdef0123456789abcdef.r2.cloudflarestorage.com"
    keys = {"access_key_id": "ak", "secret_access_key": "sk", "region": "auto"}

    assert "needs an endpoint" in preflight("s3", {"provider": "Cloudflare", **keys})
    assert "account ID" in preflight(
        "s3", {"provider": "Cloudflare", "endpoint": "https://mybucket.r2.cloudflarestorage.com", **keys}
    )
    assert "Don't put the bucket in the endpoint" in preflight(
        "s3", {"provider": "Cloudflare", "endpoint": account + "/photos", **keys}
    )
    assert "'auto'" in preflight(
        "s3",
        {"provider": "Cloudflare", "endpoint": account, "access_key_id": "ak",
         "secret_access_key": "sk", "region": "wester1"},
    )
    # A correct configuration produces no complaint.
    assert preflight(
        "s3",
        {"provider": "Cloudflare", "endpoint": account, "access_key_id": "ak",
         "secret_access_key": "sk", "region": "auto"},
    ) == ""
    # AWS legitimately has no endpoint.
    assert preflight(
        "s3",
        {"provider": "AWS", "endpoint": "", "region": "us-east-1",
         "access_key_id": "ak", "secret_access_key": "sk"},
    ) == ""
    # Other backends are not judged by the S3 rules.
    assert preflight("b2", {"endpoint": "", "account": "", "key": ""}) == ""
    assert "secret key is missing" in preflight(
        "s3", {"provider": "Other", "endpoint": "http://x:3900", "access_key_id": "ak"}
    )


def test_rclone_errors_become_one_actionable_line():
    from app.diagnostics import explain

    assert "secret key" in explain(
        "operation error S3: ListBuckets, https response error StatusCode: 403, "
        "api error SignatureDoesNotMatch: The request signature we calculated does "
        "not match the signature you provided"
    )
    assert "resolved" in explain(
        'Get "https://x.r2.cloudflarestorage.com/?x-id=ListBuckets": dial tcp: '
        "lookup x.r2.cloudflarestorage.com: no such host"
    )
    assert "refused" in explain("dial tcp 10.0.0.5:3900: connect: connection refused")
    assert "TLS" in explain("remote error: tls: handshake failure")
    # With the R2 provider the advice is R2-specific.
    assert "account ID" in explain("https response error StatusCode: 403", "Cloudflare")
    # An unrecognised error is still surfaced, not swallowed.
    assert explain("something entirely new went wrong") == "something entirely new went wrong"
    assert explain("") == "rclone failed without a message"


def test_bucket_scoped_token_is_not_a_failure():
    """R2 tokens are bucket-scoped, so ListBuckets returns 403 even though the
    credentials are fine — that must not make the remote look broken."""
    from app.diagnostics import can_list_buckets, list_bucket_notice

    denied_listing = (
        "error listing: operation error S3: ListBuckets, exceeded maximum number "
        "of attempts, 1, https response error StatusCode: 403, api error "
        "AccessDenied: Access Denied."
    )
    assert can_list_buckets(denied_listing) is True
    assert "cannot list buckets" in list_bucket_notice()
    # A genuine credential failure is still a failure.
    assert can_list_buckets("api error SignatureDoesNotMatch: bad signature") is False
    assert can_list_buckets("StatusCode: 404 NoSuchBucket") is False


# ---------------------------------------------------------------------------
# Remote name <-> config section fidelity
# ---------------------------------------------------------------------------

def test_hyphenated_remote_name_is_written_verbatim():
    """Regression: the INI section must equal the remote name exactly.

    An earlier version normalised "-" to "_", so the config contained
    [garage_local] while rclone was asked for "garage-local:" — every command
    failed with: didn't find section in config file ("garage-local").
    """
    from app.remote_config import encode_config

    for name in ("garage-local", "my.remote", "plain_name", "backups-2024", "a-b.c-d"):
        ini = encode_config(name, "s3", {"provider": "Other", "endpoint": "http://h:9000"})
        assert f"[{name}]" in ini, f"section header mangled for {name!r}:\n{ini}"
        if "-" in name or "." in name:
            mangled = name.replace("-", "_").replace(".", "_")
            assert f"[{mangled}]" not in ini, f"still normalising {name!r}:\n{ini}"


def test_rclone_resolves_generated_section_for_hyphenated_names(tmp_path):
    """End-to-end: the config we generate must be loadable by real rclone."""
    from app.remote_config import encode_config

    base = tmp_path / "data"
    base.mkdir()
    (base / "marker.txt").write_text("hello")

    for name in ("garage-local", "my.remote", "plain_name"):
        cfg = tmp_path / f"{name}.conf"
        cfg.write_text(encode_config(name, "local", {}))
        proc = subprocess.run(
            ["rclone", "lsf", f"{name}:", "--config", str(cfg)],
            capture_output=True, text=True, timeout=60, cwd=str(base),
        )
        assert "didn't find section" not in proc.stderr, (
            f"rclone could not resolve section for {name!r}: {proc.stderr}"
        )
        assert proc.returncode == 0, f"{name}: {proc.stderr}"
        assert "marker.txt" in proc.stdout


# ---------------------------------------------------------------------------
# "That port is not an S3 API" diagnosis
# ---------------------------------------------------------------------------

# Verbatim from a real run against Garage: the UI answers GET / with HTML 200,
# so rclone cannot decode the body as S3 XML.
GARAGE_UI_HTML_ERROR = (
    "error listing: operation error S3: ListBuckets, https response error "
    "StatusCode: 200, RequestID: , HostID: , deserialization failed, failed to "
    "decode response body, XML syntax error on line 8: attribute name without "
    "= in element"
)


def test_web_page_endpoint_is_diagnosed_as_wrong_port():
    """A 200 full of HTML must read as "wrong port", not as an XML error."""
    from app.diagnostics import explain

    msg = explain(GARAGE_UI_HTML_ERROR)
    assert "not the S3 API" in msg
    assert "3900" in msg, "should name Garage's real S3 port"
    # The raw rclone text must not be what the user is shown.
    assert "XML syntax error" not in msg


def test_non_s3_response_beats_generic_status_code_rules():
    """A 404/403 full of HTML is still a web page, so this rule must win."""
    from app.diagnostics import explain

    html_404 = (
        "operation error S3: ListBuckets, https response error StatusCode: 404, "
        "deserialization failed, failed to decode response body"
    )
    assert "not the S3 API" in explain(html_404)


def test_non_s3_response_is_never_a_bucket_scoped_token():
    """A UI/proxy 403 must not be mistaken for a working R2-scoped token."""
    from app.diagnostics import can_list_buckets, explain

    ui_403 = (
        "operation error S3: ListBuckets, https response error StatusCode: 403, "
        "Forbidden, deserialization failed, failed to decode response body"
    )
    assert can_list_buckets(ui_403) is False
    assert "not the S3 API" in explain(ui_403)
    # ...while a genuine S3 403 is still treated as bucket-scoped.
    assert can_list_buckets(
        "operation error S3: ListBuckets, StatusCode: 403, api error AccessDenied"
    ) is True


def test_genuine_s3_errors_are_unaffected():
    from app.diagnostics import explain

    assert "secret key does not match" in explain("api error SignatureDoesNotMatch")
    assert "not the S3 API" not in explain("api error SignatureDoesNotMatch")
    assert "No such key" not in explain("Forbidden: No such key: GK123")[:20]


# ---------------------------------------------------------------------------
# "That field doesn't contain a key" diagnosis
# ---------------------------------------------------------------------------

GARAGE_KEY_LENGTH_ERROR = (
    "error listing: operation error S3: ListBuckets, https response error "
    "StatusCode: 400, RequestID: , HostID: , api error InvalidArgument: "
    "Credential access key has length 140, should be 32"
)


def test_key_length_error_names_both_lengths():
    """Garage reports how long the key is and how long it must be — use both."""
    from app.diagnostics import explain

    msg = explain(GARAGE_KEY_LENGTH_ERROR)
    assert "140" in msg and "32" in msg
    assert "doesn't look like" not in msg  # that is the preflight wording
    assert "Credential access key has length" not in msg  # raw rclone text hidden


def test_preflight_catches_a_pasted_token_in_the_key_field():
    from app.diagnostics import preflight

    base = {"provider": "Other", "endpoint": "http://10.10.21.79:3900", "region": "garage"}

    jwt = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxIn0.abcdef"
    assert "doesn't look like a key" in preflight("s3", {**base, "access_key_id": jwt, "secret_access_key": "s"})

    curl = "curl -H 'Authorization: Bearer abc' http://10.10.21.79:3900"
    assert "doesn't look like a key" in preflight("s3", {**base, "access_key_id": curl, "secret_access_key": "s"})

    pasted_line = "AWS_ACCESS_KEY_ID=abc123def456 https://example.com x"
    assert "doesn't look like a key" in preflight("s3", {**base, "access_key_id": pasted_line, "secret_access_key": "s"})


def test_preflight_accepts_real_looking_keys():
    """Must not fire on legitimate credentials, including long MinIO usernames."""
    from app.diagnostics import preflight

    base = {"provider": "Other", "endpoint": "http://10.0.0.5:9000", "region": ""}
    assert preflight("s3", {**base, "access_key_id": "GK" + "a1b2c3" * 5, "secret_access_key": "f" * 64}) == ""
    # A MinIO root user may be an arbitrary, fairly long string.
    assert preflight("s3", {**base, "access_key_id": "backup-service-account", "secret_access_key": "f" * 64}) == ""
    assert preflight("s3", {**base, "access_key_id": "admin@example.com", "secret_access_key": "f" * 64}) == ""


def test_secret_field_paste_is_caught_too():
    from app.diagnostics import preflight

    base = {"provider": "Other", "endpoint": "http://10.0.0.5:9000", "region": ""}
    out = preflight("s3", {**base, "access_key_id": "GK123", "secret_access_key": "eyJhbGciOi.eyJzdWIi.sig"})
    assert "secret key doesn't look like a key" in out


# ---------------------------------------------------------------------------
# Provider-correct credential labels (rclone says "AWS" for every provider)
# ---------------------------------------------------------------------------

def test_rclone_itself_calls_the_s3_keys_aws():
    """Documents why the override exists — rclone has one shared option set."""
    from app.providers import get_provider, providers_cached

    s3 = get_provider("s3")
    help_by_name = {o.name: o.help for o in s3.options}
    assert help_by_name["access_key_id"].startswith("AWS Access Key ID")
    assert help_by_name["secret_access_key"].startswith("AWS Secret Access Key")


def test_credential_labels_are_provider_specific():
    from app.presets import option_labels

    assert "AWS" in option_labels("s3", "AWS")["access_key_id"]
    cf = option_labels("s3", "Cloudflare")
    assert "R2" in cf["access_key_id"] and "AWS" not in cf["access_key_id"]
    assert "R2" in cf["secret_access_key"] and "AWS" not in cf["secret_access_key"]
    # Case-insensitive, and unknown providers fall back to neutral wording.
    assert "R2" in option_labels("s3", "cloudflare")["access_key_id"]
    for unknown in ("Minio", "Backblaze", "Garage", "SomeNewThing"):
        assert "AWS" not in option_labels("s3", unknown)["access_key_id"]
    # Only S3 has this shared-option problem.
    assert option_labels("b2", "Cloudflare") == {}


def test_garage_alias_gets_neutral_not_aws_labels():
    """Garage maps to provider 'Other', so it must not be labelled AWS."""
    from app.presets import S3_PROVIDER_ALIASES, option_labels, preset_for

    assert S3_PROVIDER_ALIASES["Garage"]["maps_to"] == "Other"
    # It keeps its own, more specific note (naming the S3 port) rather than
    # collapsing to the generic "Other" wording.
    assert "3900" in preset_for("Garage")["note"]
    # Whatever the note, the credential label must be neutral, never "AWS".
    for alias in ("Garage", "Backblaze"):
        labels = option_labels("s3", alias)
        assert "AWS" not in labels["access_key_id"], alias
        assert "AWS" not in labels["secret_access_key"], alias


def test_presets_payload_carries_labels_for_the_form():
    from app.presets import presets_payload
    from app.providers import get_provider, providers_cached

    s3 = get_provider("s3")
    payload = presets_payload(s3)
    assert set(payload["labels"]) >= {"AWS", "Cloudflare", "Other"}
    assert "R2" in payload["labels"]["Cloudflare"]["access_key_id"]

    # A non-S3 backend has no provider enum, so it ships no overrides.
    other = get_provider("b2")
    assert presets_payload(other)["labels"] == {}


def test_no_duplicate_keys_in_preset_dicts():
    """A repeated 'hide' key silently discards the earlier list."""
    import ast

    src = open("app/presets.py").read()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Dict):
            keys = [k.value for k in node.keys if isinstance(k, ast.Constant)]
            assert len(keys) == len(set(keys)), f"duplicate keys: {keys}"


# ---------------------------------------------------------------------------
# The S3 provider enum is case-sensitive (rclone: 'provider "x" not known')
# ---------------------------------------------------------------------------

def _run_with_config(tmp_path, body):
    cfg = tmp_path / "r.conf"
    cfg.write_text(body)
    return subprocess.run(
        ["rclone", "lsd", "x:", "--config", str(cfg), "--retries", "1"],
        capture_output=True, text=True, timeout=90,
    )


def test_rclone_rejects_a_wrong_case_provider(tmp_path):
    """Documents the real rclone behaviour this guards against."""
    proc = _run_with_config(
        tmp_path,
        "[x]\ntype = s3\nprovider = cloudflare\nregion = auto\n"
        "access_key_id = a\nsecret_access_key = b\n",
    )
    err = proc.stderr + proc.stdout
    assert 'provider "cloudflare" not known' in err, err[-400:]
    # The exact-case spelling gets past that check (it then fails on the network).
    proc2 = _run_with_config(
        tmp_path,
        "[x]\ntype = s3\nprovider = Cloudflare\nregion = auto\n"
        "access_key_id = a\nsecret_access_key = b\n",
    )
    assert 'not known' not in proc2.stderr + proc2.stdout


def test_preflight_flags_wrong_case_provider(s3_loaded):
    from app.diagnostics import preflight

    base = {
        "endpoint": "https://cea6c82c5dec1a535239018dd6a8f8f2.r2.cloudflarestorage.com",
        "region": "auto", "access_key_id": "a" * 32, "secret_access_key": "b" * 64,
    }
    msg = preflight("s3", {**base, "provider": "cloudflare"})
    assert "case-sensitive" in msg
    assert "Cloudflare" in msg, "must name the exact spelling to use"
    # Correctly cased passes this check (it may still fail for other reasons).
    assert "case-sensitive" not in preflight("s3", {**base, "provider": "Cloudflare"})


def test_preflight_flags_a_wholly_unknown_provider(s3_loaded):
    from app.diagnostics import preflight

    msg = preflight("s3", {"provider": "Dropbox", "endpoint": "http://x:1",
                            "access_key_id": "a", "secret_access_key": "b"})
    assert "not an S3 provider" in msg
    # Only s3 has a provider enum.
    assert preflight("b2", {"provider": "nonsense"}) == ""


def test_blank_provider_is_allowed():
    """An empty provider is valid — rclone falls back to its default."""
    from app.diagnostics import preflight

    assert preflight("s3", {"endpoint": "http://h:9000", "access_key_id": "a",
                            "secret_access_key": "b"}) == ""


def test_canonical_provider_lookup(s3_loaded):
    from app.diagnostics import canonical_s3_provider, s3_provider_values

    allowed = s3_provider_values()
    if not allowed:
        return  # catalogue not loaded in this process
    assert canonical_s3_provider("cloudflare") == "Cloudflare"
    assert canonical_s3_provider("CLOUDFLARE") == "Cloudflare"
    assert canonical_s3_provider("Cloudflare") == "Cloudflare"
    assert canonical_s3_provider("nope") == ""
    assert canonical_s3_provider("") == ""
    assert "Cloudflare" in allowed and "cloudflare" not in allowed


# ---------------------------------------------------------------------------
# Region dropdown choices
# ---------------------------------------------------------------------------

def test_region_choices_put_the_provider_default_first(s3_loaded):
    from app.presets import region_choices

    assert region_choices(s3_loaded, "Cloudflare")[0]["value"] == "auto"
    assert region_choices(s3_loaded, "Garage")[0]["value"] == "garage"
    assert region_choices(s3_loaded, "AWS")[0]["value"] == "us-east-1"
    assert region_choices(s3_loaded, "Minio")[0]["value"] == "us-east-1"


def test_region_choices_offer_real_regions_with_no_duplicates(s3_loaded):
    from app.presets import region_choices

    values = [c["value"] for c in region_choices(s3_loaded, "AWS")]
    assert len(values) == len(set(values)), "duplicate region offered"
    assert len(values) > 50, values
    for expected in ("us-east-1", "eu-west-1", "eu-central-1", "ap-southeast-1"):
        assert expected in values, expected
    # Non-S3 backends have no curated regions, and a missing spec is safe.
    assert region_choices(None, "AWS") == []


def test_presets_payload_has_regions_for_every_preset(s3_loaded):
    from app.presets import presets_payload

    payload = presets_payload(s3_loaded)
    assert set(payload["presets"]) <= set(payload["regions"])
    assert payload["regions"]["Cloudflare"][0]["value"] == "auto"
    assert payload["regions"]["Garage"][0]["value"] == "garage"
    # A blank provider (the default) must also have a list.
    assert payload["regions"][""], "no regions for an unset provider"
    # Non-S3 backends carry none.
    from app.providers import get_provider

    assert presets_payload(get_provider("b2"))["regions"] == {}


def test_saved_provider_and_region_are_canonicalised(s3_loaded):
    """Wrong case must not reach the rclone config."""
    from app.remote_config import normalize_options

    assert normalize_options({"provider": "cloudflare"}, "s3")["provider"] == "Cloudflare"
    assert normalize_options({"provider": "CLOUDFLARE"}, "s3")["provider"] == "Cloudflare"
    assert normalize_options({"region": "AUTO"}, "s3")["region"] == "auto"
    assert normalize_options({"region": "GARAGE"}, "s3")["region"] == "garage"
    assert normalize_options({"region": "US-EAST-1"}, "s3")["region"] == "us-east-1"
    # An unknown provider is left for preflight to report, not silently rewritten.
    assert normalize_options({"provider": "Dropbox"}, "s3")["provider"] == "Dropbox"


def test_custom_regions_are_never_rewritten(s3_loaded):
    """Self-hosted servers legitimately use region names rclone has not heard of."""
    from app.remote_config import normalize_options

    for custom in ("my-cluster", "dc1-lab", "on-prem-01"):
        out = normalize_options({"provider": "Other", "region": custom}, "s3")
        assert out["region"] == custom, custom
    # A real region in the wrong case is still corrected, even against Other.
    assert normalize_options({"provider": "Other", "region": "GARAGE"}, "s3")["region"] == "garage"
    # Non-S3 backends are untouched entirely.
    assert normalize_options({"region": "WEIRD"}, "b2") == {"region": "WEIRD"}


# ---------------------------------------------------------------------------
# rclone's example JSON casing (every dropdown depends on this)
# ---------------------------------------------------------------------------

def test_rclone_emits_capitalised_example_keys(s3_loaded):
    """Documents rclone's real shape, which the parser must accept."""
    proc = subprocess.run(
        ["rclone", "config", "providers"], capture_output=True, timeout=120,
    )
    raw = json.loads(proc.stdout.decode(errors="replace"))
    # rclone emits a bare list; the {"providers": [...]} wrapper is our own.
    providers = raw["providers"] if isinstance(raw, dict) else raw
    s3_raw = next(p for p in providers if p["Name"] == "s3")
    prov = next(o for o in s3_raw["Options"] if o["Name"] == "provider")
    example = (prov.get("Examples") or [])[0]
    assert "Value" in example, f"unexpected casing: {sorted(example)}"


def test_cache_round_trip_preserves_examples(tmp_path, catalogue):
    """The bug: our own cache wrote lowercase, the reader wanted capitalised.

    Anything read from the cache lost every example, so the form rendered
    plain text boxes instead of dropdowns after a restart.
    """
    import app.providers as P

    written = P._write_cache
    try:
        P._write_cache = lambda providers, version: _dump_cache(
            tmp_path, providers, version
        )
        P.reset_cache()
        real_cache_path = P.cache_path
        P.cache_path = lambda: tmp_path / ".rclone_providers.json"
        loaded = asyncio.run(P.load_catalogue(force_refresh=True))
        s3 = next(p for p in loaded if p.name == "s3")
        by_name = {o.name: o for o in s3.options}
        assert len(by_name["provider"].examples) == 53, len(by_name["provider"].examples)
        assert len(by_name["region"].examples) == 128, len(by_name["region"].examples)
    finally:
        P._write_cache = written
        P.cache_path = real_cache_path
        P.reset_cache()


def _dump_cache(tmp_path, providers, version):
    """A local stand-in for _write_cache, using the real serialisation."""
    from app.providers import _parse

    payload = {
        "generated_at": __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc
        ).isoformat(),
        "rclone_version": version,
        "providers": [
            {
                "Name": p.name,
                "Description": p.description,
                "Aliases": p.aliases,
                "Hide": p.hidden,
                "Overview": {"Tier": p.tier, "Features": p.features},
                "Options": [
                    {
                        "Name": o.name, "Type": o.type, "Required": o.required,
                        "IsPassword": o.password, "Advanced": o.advanced,
                        "DefaultStr": o.default, "Help": o.help,
                        "Examples": o.examples,
                    }
                    for o in p.options
                ],
            }
            for p in providers
        ],
    }
    (tmp_path / ".rclone_providers.json").write_text(json.dumps(payload))
    # Exercise the real reader against what the real writer produces.
    assert _parse(payload["providers"])


def test_provider_and_region_survive_parsing(s3_loaded):
    """The regression: examples were dropped, so no dropdown rendered at all."""
    s3 = get_provider("s3")
    by_name = {o.name: o for o in s3.options}
    assert len(by_name["provider"].examples) == 53, len(by_name["provider"].examples)
    assert len(by_name["region"].examples) == 128, len(by_name["region"].examples)
    # And they must survive the trip to the browser as dicts with a value.
    detail = {o["name"]: o for o in s3.detail()["options"]}
    assert len(detail["provider"]["examples"]) == 53
    assert detail["provider"]["examples"][0]["value"] == "AWS"
    assert detail["provider"]["examples"][0]["help"], "help text was lost too"
    assert all(e.get("value") for e in detail["region"]["examples"])


def test_parser_accepts_both_example_casings():
    """Older rclone used Value/Help; both must work."""
    from app.providers import _parse

    def opt(examples):
        return {
            "Name": "s3", "Description": "d", "Aliases": [], "Hide": False,
            "Overview": {"Tier": "Tier 1", "Features": []},
            "Options": [{
                "Name": "provider", "Type": "string", "Required": True,
                "IsPassword": False, "Advanced": False, "DefaultStr": "",
                "Help": "h", "Examples": examples,
            }],
        }

    lower = _parse([opt([{"value": "AWS", "help": "Amazon"}])])
    upper = _parse([opt([{"Value": "AWS", "Help": "Amazon"}])])
    for parsed in (lower, upper):
        examples = parsed[0].options[0].examples
        assert examples == [{"value": "AWS", "help": "Amazon"}], examples


def test_every_backend_with_enums_actually_has_options(catalogue):
    """Guards the whole class: a silently empty enum is a broken dropdown.

    Scoped to the S3 backend: its provider/region/storage_class/acl are the
    closed enums the form turns into dropdowns. Other backends legitimately
    declare free-text fields under the same names (Oracle and Swift accept any
    region), so asserting on them would be wrong.
    """
    empties = []
    s3 = get_provider("s3")
    assert s3 is not None
    for name in ("provider", "region", "storage_class", "acl"):
        option = next((o for o in s3.options if o.name == name), None)
        if option is None:
            continue
        if not option.examples:
            empties.append(f"s3.{name}")
    assert not empties, f"closed enums parsed with no values: {empties}"


def test_valid_provider_is_never_flagged_when_catalogue_is_empty(s3_loaded, monkeypatch):
    """Regression: a valid 'Cloudflare' was rejected with "spelled ''".

    When the enum could not be read, canonical_s3_provider() returned "" and
    preflight treated that as a wrong value, inventing a message that named no
    correct spelling. An unreadable catalogue must produce no complaint.
    """
    import app.diagnostics as D

    monkeypatch.setattr(D, "s3_provider_values", lambda: set())
    base = {
        "endpoint": "https://cea6c82c5dec1a535239018dd6a8f8f2.r2.cloudflarestorage.com",
        "region": "auto", "access_key_id": "a" * 32, "secret_access_key": "b" * 64,
    }
    for good in ("Cloudflare", "AWS", "Minio", "Other"):
        assert D.preflight("s3", {**base, "provider": good}) == "", good
    # And no message may ever name an empty spelling.
    for name in ("Cloudflare", "AWS", "Minio", "Other", "Garage"):
        msg = D.preflight("s3", {**base, "provider": name})
        assert "spelled ''" not in msg, (name, msg)


def test_cache_format_version_invalidates_old_caches(tmp_path, monkeypatch):
    """A cache from an older build must be ignored, not half-read."""
    import app.providers as P

    monkeypatch.setattr(P, "cache_path", lambda: tmp_path / "c.json")
    stale = {
        "generated_at": "2026-01-01T00:00:00+00:00",
        "rclone_version": "v1.75.0",  # no format_version: an older build
        "providers": [{"Name": "s3", "Options": [
            {"Name": "provider", "Type": "string", "Examples": [{"Value": "AWS"}]}
        ]}],
    }
    (tmp_path / "c.json").write_text(json.dumps(stale))
    assert P._read_cache() is None


# ---------------------------------------------------------------------------
# Bucket-scoped tokens (the normal Cloudflare R2 case)
# ---------------------------------------------------------------------------

R2_403_LISTBUCKETS = (
    "error listing: operation error S3: ListBuckets, https response error "
    "StatusCode: 403, RequestID: , HostID: ,api error AccessDenied: Access Denied"
)
# The error a wrongly pasted key produces, verbatim from R2/Garage.
R2_KEY_LENGTH = (
    "error listing: operation error S3: ListBuckets, https response error "
    "StatusCode: 400, RequestID: , HostID: , api error InvalidArgument: "
    "Credential access key has length 140, should be 32"
)


def test_a_403_on_listbuckets_is_not_a_broken_remote():
    from app.diagnostics import can_list_buckets, explain, list_bucket_notice

    assert can_list_buckets(R2_403_LISTBUCKETS) is True
    assert "cannot list buckets" in list_bucket_notice()
    # It must never be reported as a credential failure.
    assert "does not match" not in explain(R2_403_LISTBUCKETS, "Cloudflare")


def test_a_400_on_key_length_is_a_real_failure():
    """Unlike 403, this means the key really is wrong."""
    from app.diagnostics import can_list_buckets, explain

    assert can_list_buckets(R2_KEY_LENGTH) is False
    msg = explain(R2_KEY_LENGTH)
    assert "140" in msg and "32" in msg


def test_bucket_scoped_notice_names_the_bucket_and_contents():
    from app.diagnostics import bucket_scoped_notice

    msg = bucket_scoped_notice("test-bucket", ["garage-local-backup"])
    assert "test-bucket" in msg
    assert "garage-local-backup" in msg, "should show what is actually in there"
    assert "cannot list buckets" in msg
    # An empty bucket must read clearly too, not as a broken listing.
    assert "empty" in bucket_scoped_notice("test-bucket", [])


def test_credentials_long_enough_to_explain_themselves():
    """A bad paste must reach the diagnostics, not be rejected as a 422."""
    from app.schemas import RemoteCreate, RemoteUpdate

    long_value = "x" * 140
    assert RemoteCreate(name="n", endpoint="http://h:1",
                        access_key=long_value).access_key == long_value
    assert RemoteUpdate(access_key=long_value).access_key == long_value


def test_bucket_is_accepted_and_clearable():
    from app.schemas import RemoteCreate, RemoteUpdate

    assert RemoteCreate(name="n", endpoint="http://h:1", bucket="b").bucket == "b"
    assert RemoteCreate(name="n", endpoint="http://h:1").bucket == ""
    # An empty string must be distinguishable from "not supplied", so the
    # bucket can be cleared on edit.
    assert RemoteUpdate(bucket="").bucket == ""
    assert RemoteUpdate().bucket is None


def test_saved_bucket_becomes_the_remote_root():
    """Without this a bucket-scoped remote lists nothing, ever."""
    from app.rclone import _build_target

    # _resolve_target needs a DB row; assert the rule it implements.
    assert _build_target("r2", "test-bucket") == "r2:test-bucket"
    assert _build_target("r2", "") == "r2:"


# ---------------------------------------------------------------------------
# A hung endpoint must fail with a message, never spin forever
# ---------------------------------------------------------------------------

def test_run_rclone_times_out_and_reports(tmp_path):
    """A server that accepts then stalls used to hang the request forever."""
    import asyncio as _aio
    import time as _time

    import app.rclone as R

    original = R.RCLONE_TIMEOUT
    R.RCLONE_TIMEOUT = 6.0
    server = subprocess.Popen(
        [sys.executable, "-c",
         "import time\n"
         "from http.server import BaseHTTPRequestHandler, HTTPServer\n"
         "class H(BaseHTTPRequestHandler):\n"
         "    def log_message(self,*a): pass\n"
         "    def do_GET(self):\n"
         "        time.sleep(60)\n"
         "HTTPServer(('127.0.0.1', 8997), H).serve_forever()\n"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        _time.sleep(2)
        cfg = tmp_path / "c.conf"
        # A real remote pointing at the stalling server, so rclone blocks on
        # the HTTP request rather than failing on a malformed target.
        cfg.write_text(
            "[slow]\ntype = s3\nprovider = Other\n"
            "endpoint = http://127.0.0.1:8997\nregion = x\n"
            "access_key_id = k\nsecret_access_key = s\n"
        )
        started = _time.time()

        async def go():
            return await R._run_rclone(
                str(cfg), ["lsf", "slow:", "--max-depth", "1"]
            )

        code, out, err = _aio.run(go())
        elapsed = _time.time() - started
        assert code == 1, code
        assert "did not finish" in err, err[:200]
        assert "never replied" in err or "did not respond" in err, err[:200]
        assert elapsed < 30, f"took {elapsed:.0f}s — the timeout is not working"
    finally:
        server.terminate()
        server.wait(timeout=10)
        R.RCLONE_TIMEOUT = original


def test_transfers_are_not_subject_to_the_interactive_timeout():
    """Long transfers must never be cut short by the Test First ceiling."""
    import inspect

    import app.rclone as R

    src = inspect.getsource(R._run_rclone)
    # The budget is a parameter defaulting to the interactive ceiling, and
    # `timeout=None` disables it entirely.
    assert "timeout: float | None = None" in src
    assert "if budget is None:" in src
    # Transfers use their own spawn path, not _run_rclone.
    transfer = Path(__file__).resolve().parents[1] / "app" / "services" / "transfer.py"
    assert "_run_rclone" not in transfer.read_text()


def test_received_summary_reports_length_but_never_the_secret():
    """The user must be able to check a length complaint against what was sent."""
    from app.routers.remotes import _describe_received
    from app.schemas import RemoteCreate

    payload = RemoteCreate(
        name="n", endpoint="https://x.r2.cloudflarestorage.com",
        options={
            "access_key_id": "eyJhbGciOiJIUzI1NiJ9" + "x" * 120,
            "secret_access_key": "s" * 64,
        },
    )
    out = _describe_received(payload, payload.options)
    assert "140 chars" in out, out
    assert "eyJhbG" in out, "a masked prefix helps identify a JWT/token"
    # The secret must never be echoed, not even partially.
    assert "s" * 64 not in out
    assert "secret_access_key" not in out
    # Only the secret's *length* is reported, never its value.
    assert "secret key: 64 chars" in out
    assert "secret key: s" not in out
    # With no credentials at all, only the endpoint is reported (it is required
    # by the schema, so the summary is never empty).
    blank = RemoteCreate(name="n", endpoint="http://h:1")
    out2 = _describe_received(blank, {})
    assert "access key id" not in out2 and "secret key" not in out2, out2


def test_received_summary_falls_back_to_the_legacy_columns():
    from app.routers.remotes import _describe_received
    from app.schemas import RemoteCreate

    payload = RemoteCreate(
        name="n", endpoint="http://h:1", access_key="a" * 32, secret_key="b" * 64
    )
    out = _describe_received(payload, {})
    assert "32 chars" in out and "64 chars" in out, out


# ---------------------------------------------------------------------------
# Secrets must be decrypted before rclone sees them
# ---------------------------------------------------------------------------

def test_encrypted_credentials_never_reach_rclone():
    """Regression: a 32-char key became a 140-char Fernet token on the wire.

    normalize_options() encrypts secret options for storage at rest. The inline
    "Test First" route passed that map straight through to the generated rclone
    config, so rclone authenticated with the ciphertext. Cloudflare rejected it
    with "Credential access key has length 140, should be 32" — a message about
    our encryption, not about anything the user had typed.
    """
    from app.remote_config import (
        allowed_option_names,
        decrypt_options,
        normalize_options,
    )

    raw = {
        "provider": "Cloudflare",
        "region": "auto",
        "access_key_id": "a" * 32,
        "secret_access_key": "b" * 64,
    }
    stored = normalize_options(raw, "s3", allowed_option_names("s3"))
    # Stored form is encrypted, and that is what the database must hold.
    assert stored["access_key_id"] != "a" * 32
    assert len(stored["access_key_id"]) == 140, len(stored["access_key_id"])

    # What the test path must use instead.
    usable = decrypt_options(stored, "s3")
    assert usable["access_key_id"] == "a" * 32
    assert usable["secret_access_key"] == "b" * 64
    assert usable["provider"] == "Cloudflare"


def test_fernet_length_explains_the_140_character_complaint():
    """Pins the arithmetic behind the confusing provider error."""
    from app.crypto import encrypt

    lengths = {len(encrypt("x" * n)) for n in (20, 32)}
    # A 32-character S3 key id encrypts to exactly 140 characters, which is the
    # number Cloudflare reported. This is what identified the bug.
    assert 140 in lengths, lengths


# ---------------------------------------------------------------------------
# Bucket pickers: the transfer console never received the remote's name
# ---------------------------------------------------------------------------

def test_transfer_bucket_endpoint_accepts_the_names_htmx_actually_sends():
    """Regression: "Failed to load buckets" on the Transfer console.

    htmx sends the triggering <select>'s own name — source_remote or
    dest_remote — but this endpoint only accepted `remote`, so it always got an
    empty name and reported a failure for a perfectly healthy remote.
    """
    import inspect

    from app.routers.transfers import transfers_buckets

    sig = set(inspect.signature(transfers_buckets).parameters)
    assert {"remote", "source_remote", "dest_remote"} <= sig, sig
    src = inspect.getsource(transfers_buckets)
    assert "source_remote or dest_remote" in src, src


def test_job_bucket_endpoint_accepts_the_same_names():
    import inspect

    from app.routers.jobs import bucket_options

    sig = set(inspect.signature(bucket_options).parameters)
    assert {"remote", "source_remote", "dest_remote"} <= sig, sig


def test_bucket_choices_falls_back_to_the_saved_bucket():
    """A bucket-scoped R2 token cannot list; the saved bucket is the answer."""
    import asyncio as _aio

    import app.bucket_choices as bc

    class Row:
        bucket = "test-bucket"

    async def fake_row(name):
        return Row() if name == "cloudflare" else None

    async def boom(name):
        raise RuntimeError("403 AccessDenied")

    original_row, original_list = bc._remote_row, bc.list_buckets
    bc._remote_row, bc.list_buckets = fake_row, boom
    try:
        buckets, note, error = _aio.run(bc.bucket_choices("cloudflare"))
        assert buckets == ["test-bucket"], buckets
        assert error == "", error
        assert "cannot list buckets" in note, note
        assert "test-bucket" in note
        # No saved bucket: say so plainly instead of pretending it is broken.
        class Empty:
            bucket = ""
        bc._remote_row = lambda name: _aio.sleep(0, result=Empty())
        buckets, note, error = _aio.run(bc.bucket_choices("cloudflare"))
        assert buckets == []
        assert "no bucket saved" in note, note
        # No remote chosen at all.
        buckets, note, error = _aio.run(bc.bucket_choices(""))
        assert buckets == [] and "remote" in note.lower()
    finally:
        bc._remote_row, bc.list_buckets = original_row, original_list


def test_bucket_scoped_notice_points_at_the_bucket_field():
    """The old wording sent users to Jobs, which cannot work without a bucket."""
    from app.diagnostics import list_bucket_notice

    notice = list_bucket_notice()
    assert "Bucket field" in notice, notice
    assert "just type the bucket name in Jobs" not in notice


def test_bucket_options_template_never_says_failed_for_a_healthy_remote():
    from pathlib import Path

    tpl = (Path(__file__).resolve().parents[1] / "app" / "templates"
           / "components" / "bucket_options.html").read_text()
    assert "Failed to load buckets" not in tpl, tpl
    assert "{{ note }}" in tpl and "{{ error" in tpl


def test_list_buckets_ignores_the_saved_bucket():
    """Regression: a saved bucket made list_buckets return its contents.

    The saved bucket is the root for transfers, but listing buckets has to come
    from the remote root — otherwise a bucket-scoped remote offered its own
    objects ("garage-local-backup") as if they were buckets, twice over.
    """
    import inspect

    from app.rclone import list_buckets

    src = inspect.getsource(list_buckets)
    assert "_resolve_target" not in src, "list_buckets must not use the bucket default"
    assert "_build_target(remote, \"\")" in src, src
    # And it must de-duplicate.
    assert "seen" in src, src
