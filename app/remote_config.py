"""Remote option handling: validation, encryption at rest and INI rendering.

A remote is an rclone backend plus a set of config options. Options live in
``remotes.options`` as JSON; the legacy ``endpoint``/``access_key``/
``secret_key``/``region``/``provider`` columns are merged in as a compatibility
layer so remotes created before this feature keep working untouched.

Everything here is deliberately strict: option names must look like rclone
config keys and values may not contain newlines or control characters, so a
stored value can never break out of its ``key = value`` line and inject a new
INI section into the generated rclone config.
"""

import json
import re

from app.crypto import decrypt, encrypt, is_encrypted
from app.providers import get_provider, secret_option_names

MAX_VALUE_LENGTH = 4096
# rclone config keys are [a-z0-9_]; a few backends use upper case, so allow it.
_KEY_RE = re.compile(r"^[A-Za-z0-9_]+$")

# Legacy column -> rclone option name, used to merge the old shape into the
# option map. Deliberately not `type`: the backend is written separately.
LEGACY_S3_MAP = {
    "endpoint": "endpoint",
    "access_key": "access_key_id",
    "secret_key": "secret_access_key",
    "region": "region",
    "provider": "provider",
}


class OptionError(ValueError):
    """Raised for an invalid option name or value (surfaces as HTTP 400)."""


def parse_options(raw) -> dict:
    """Decode the stored JSON option map, tolerating junk/legacy rows."""
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def validate_option(name: str, value) -> tuple:
    """Return (name, value) or raise OptionError. Rejects INI injection."""
    key = (name or "").strip()
    if not _KEY_RE.match(key):
        raise OptionError(
            f"invalid option name '{key}': only letters, digits and '_' are allowed"
        )
    if isinstance(value, bool):
        value = "true" if value else "false"
    val = ("" if value is None else str(value)).strip()
    if len(val) > MAX_VALUE_LENGTH:
        raise OptionError(f"option '{key}' is too long (max {MAX_VALUE_LENGTH} chars)")
    if any(ch in val for ch in ("\n", "\r", "\x00")):
        raise OptionError(f"invalid value for '{key}': newlines are not allowed")
    if any(ord(ch) < 32 for ch in val):
        raise OptionError(
            f"invalid value for '{key}': control characters are not allowed"
        )
    return key, val


def normalize_options(raw: dict, backend: str, allowed: set = None) -> dict:
    """Validate + encrypt a submitted option map for storage.

    `allowed` optionally restricts accepted keys to the provider's own option
    names, so a typo is reported instead of being silently written into the
    generated rclone config. Unknown backends keep accepting free-form keys
    (forward compatibility with a newer rclone than the cached catalogue).
    """
    if not isinstance(raw, dict):
        raise OptionError("options must be an object")
    secrets = secret_option_names(backend)
    clean: dict = {}
    for name, value in raw.items():
        if value is None:
            continue
        key, val = validate_option(name, value)
        if allowed is not None and key not in allowed:
            raise OptionError(f"'{key}' is not an option of the '{backend}' backend")
        if not val:
            continue  # never store empties: rclone applies its own default
        clean[key] = encrypt(val) if key in secrets else val
    _canonicalize(backend, clean)
    return clean


def _canonicalize(backend: str, clean: dict) -> None:
    """Fix the case of values rclone matches case-sensitively, in place.

    The provider enum is the important one ('cloudflare' is rejected where
    'Cloudflare' works), but a region typed as 'US-EAST-1' or 'AUTO' is the
    same class of mistake, so it is corrected to the catalogue's spelling.
    Values not in the catalogue are left untouched: self-hosted S3 servers
    legitimately use region names rclone has never heard of.
    """
    if backend != "s3":
        return
    try:
        from app.diagnostics import canonical_s3_provider
        from app.presets import region_choices
        from app.providers import get_provider

        provider = get_provider("s3")
        if provider is None:
            return
        entered = clean.get("provider")
        if entered:
            canonical = canonical_s3_provider(entered)
            if canonical:
                clean["provider"] = canonical
        entered = clean.get("region")
        if entered:
            # Match against every region any provider can ask for, not just the
            # selected provider's, so 'GARAGE' is still corrected when the
            # provider was left blank.
            known: set = set()
            for name in ("", "Cloudflare", "AWS", "Minio", "Other", "Garage"):
                known.update(c["value"] for c in region_choices(provider, name))
            for value in known:
                if value.lower() == entered.lower():
                    clean["region"] = value
                    break
    except Exception:  # never block a save on a cosmetic fix-up
        return


def decrypt_options(options: dict, backend: str) -> dict:
    """Decrypt any encrypted values in a stored option map."""
    secrets = secret_option_names(backend)
    out: dict = {}
    for key, value in options.items():
        if isinstance(value, str) and key in secrets and is_encrypted(value):
            try:
                value = decrypt(value)
            except Exception:  # noqa: BLE001 - a rotated key must not 500 the UI
                continue
        out[key] = value
    return out
def legacy_options(backend: str, row) -> dict:
    """Option map implied by the pre-existing columns of an S3 remote.

    Only `s3` uses them: `local` keeps its absolute path in `endpoint` (rclone's
    local backend has no root option — the path is inlined into the target),
    and every other backend is fully described by `options`.
    """
    if backend != "s3":
        return {}
    out = {}
    for column, option in LEGACY_S3_MAP.items():
        value = (getattr(row, column, "") or "").strip()
        if value:
            out[option] = value
    for option in ("access_key_id", "secret_access_key"):
        if option in out:
            out[option] = decrypt(out[option])
    return out


def resolve_options(row) -> dict:
    """The full, decrypted option map for a Remote row (legacy + new)."""
    backend = (row.type or "s3").strip()
    merged = legacy_options(backend, row)
    merged.update(decrypt_options(parse_options(row.options), backend))
    return {k: v for k, v in merged.items() if v not in (None, "")}


def allowed_option_names(backend: str):
    """The backend's real option names, or None when the catalogue is unknown."""
    provider = get_provider(backend)
    if provider is None:
        return None
    return {o.name for o in provider.options} | {"provider"}


def encode_config(name: str, backend: str, options: dict) -> str:
    """Render one rclone config section.

    Values are quoted only when rclone requires it (embedded `#`, quotes or
    surrounding whitespace). Everything passes through `validate_option` first,
    so a value can never escape its line or start a new section.
    """
    # rclone resolves section names verbatim, so the section MUST match the
    # remote name exactly (rclone accepts letters, digits, "_", "-" and ".").
    # Normalising "-" or "." to "_" here would produce a config that rclone
    # then reports as "didn't find section in config file" for every command.
    # Names are already validated as ^[a-z0-9_-]+$; this is a safety net for
    # characters that genuinely cannot appear in an INI section header.
    section = re.sub(r"[^A-Za-z0-9_.-]", "_", name)
    lines = [f"[{section}]", f"type = {backend}"]
    for key in sorted(options):
        val = str(options[key])
        if "#" in val or val[:1] in ("'", '"') or val != val.strip():
            val = val.replace("\\", "\\\\").replace('"', '\\"')
            val = f'"{val}"'
        lines.append(f"{key} = {val}")
    lines.append("")
    return "\n".join(lines)


def options_summary(row) -> list:
    """Options for the UI: secret values are never returned, only `set`."""
    backend = (row.type or "s3").strip()
    secrets = secret_option_names(backend)
    out = []
    for key, value in sorted(parse_options(row.options).items()):
        out.append(
            {
                "name": key,
                "value": None if key in secrets else value,
                "secret": key in secrets,
                "set": bool(value),
            }
        )
    return out