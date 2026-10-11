"""rclone backend catalogue.

The set of usable rclone backends is discovered from rclone itself rather than
hardcoded: ``rclone config providers`` prints a JSON description of every
backend (69 in rclone 1.75), including each option's name, type, whether it is
required, whether it holds a secret, whether it is advanced, and its default.

This module normalises that output into a small, stable structure used by the
remote form (which renders itself from it) and by the config writer (which needs
to know which option values are secrets). The catalogue is cached in the data
dir so the UI is instant on later boots and works offline.
"""

import asyncio
import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from app.config import settings

CACHE_TTL = timedelta(hours=24)
CACHE_FILENAME = ".rclone_providers.json"
# Bump when the shape written by _write_cache changes, so a cache written by an
# older build is discarded instead of being read back with fields missing.
# (Version 2 fixed the Examples key casing, which silently emptied every
# dropdown in the form.)
CACHE_FORMAT_VERSION = 2

# Backends that wrap another remote (they take a `remote` option instead of
# credentials). The UI marks them so a user does not try to enter access keys.
WRAPPER_BACKENDS = {
    "alias",
    "chunker",
    "combine",
    "compress",
    "crypt",
    "tiered",
    "union",
}

# Feature flag rclone sets for backends that expose named containers. It decides
# whether a bucket dropdown makes sense for a remote.
BUCKET_BASED_FEATURE = "BucketBased"

# Names rclone does not flag as password but which are secrets regardless.
SECRET_NAMES = {
    "access_key_id",
    "secret_access_key",
    "secret_key",
    "service_account_credentials",
    "token",
    "client_id",
    "key",
    "key2",
    "pass",
    "password",
    "password2",
    "bearer_token",
    "sas_token",
    "session_token",
    "private_key",
}


@dataclass(frozen=True)
class ProviderOption:
    """One rclone config option of a backend."""

    name: str
    type: str = "string"
    required: bool = False
    password: bool = False
    advanced: bool = False
    default: str = ""
    help: str = ""
    examples: list = field(default_factory=list)

    @property
    def is_secret(self) -> bool:
        return self.password or self.name in SECRET_NAMES

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "type": self.type,
            "required": self.required,
            "password": self.is_secret,
            "advanced": self.advanced,
            "default": self.default,
            "help": self.help,
            "examples": self.examples,
        }


@dataclass(frozen=True)
class Provider:
    """One rclone backend."""

    name: str
    description: str = ""
    tier: str = ""
    aliases: list = field(default_factory=list)
    hidden: bool = False
    wrapper: bool = False
    bucket_based: bool = False
    features: list = field(default_factory=list)
    options: list = field(default_factory=list)

    def summary(self) -> dict:
        """Lightweight payload for the backend picker (no option list)."""
        return {
            "name": self.name,
            "description": self.description,
            "tier": self.tier,
            "aliases": self.aliases,
            "wrapper": self.wrapper,
            "bucket_based": self.bucket_based,
        }

    def detail(self) -> dict:
        data = self.summary()
        data["features"] = self.features
        data["options"] = [o.to_dict() for o in self.options]
        return data


def _parse(raw: list) -> list:
    """Normalise rclone's raw provider JSON into Provider dataclasses."""
    providers = []
    for item in raw:
        name = (item.get("Name") or "").strip()
        if not name:
            continue
        overview = item.get("Overview") or {}
        features = [f for f in (overview.get("Features") or []) if isinstance(f, str)]
        options = []
        for opt in item.get("Options") or []:
            opt_name = (opt.get("Name") or "").strip()
            if not opt_name:
                continue
            raw_examples = opt.get("Examples")
            examples = []
            if isinstance(raw_examples, list):
                # rclone emits "Value"/"Help", but this app's own cache writes
                # the already-normalised lowercase dicts back out verbatim — so
                # a cache round trip would otherwise drop every dropdown.
                examples = [
                    {
                        "value": e.get("Value") or e.get("value"),
                        "help": e.get("Help") or e.get("help"),
                    }
                    for e in raw_examples
                    if isinstance(e, dict) and (e.get("Value") or e.get("value"))
                ][:128]
            options.append(
                ProviderOption(
                    name=opt_name,
                    type=opt.get("Type") or "string",
                    required=bool(opt.get("Required")),
                    password=bool(opt.get("IsPassword")),
                    advanced=bool(opt.get("Advanced")),
                    default=str(opt.get("DefaultStr") or ""),
                    help=(opt.get("Help") or "").strip(),
                    examples=examples,
                )
            )
        providers.append(
            Provider(
                name=name,
                description=(item.get("Description") or "").strip(),
                tier=overview.get("Tier") or "",
                aliases=list(item.get("Aliases") or []),
                hidden=bool(item.get("Hide")),
                wrapper=name in WRAPPER_BACKENDS,
                bucket_based=BUCKET_BASED_FEATURE in features,
                features=sorted(features),
                options=options,
            )
        )
    # Tier 1 first (most used), then alphabetically; hidden backends last.
    providers.sort(key=lambda p: (p.hidden, p.tier or "Tier 9", p.name))
    return providers
def cache_path():
    return settings.data_dir / CACHE_FILENAME


def _read_cache():
    """(providers, generated_at) from the on-disk cache, or None.

    A cache written by a different CACHE_FORMAT_VERSION is ignored outright:
    it may be missing fields this build needs, and a half-understood cache is
    worse than none.
    """
    try:
        data = json.loads(cache_path().read_text())
        if data.get("format_version") != CACHE_FORMAT_VERSION:
            return None
        generated = datetime.fromisoformat(data["generated_at"])
        if datetime.now(timezone.utc) - generated > CACHE_TTL:
            return None
        return _parse(data.get("providers") or []), generated
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _write_cache(providers: list, version: str) -> None:
    """Persist only the fields the app consumes (rclone's dump is ~1 MB)."""
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "rclone_version": version,
        "format_version": CACHE_FORMAT_VERSION,
        "providers": [
            {
                "Name": p.name,
                "Description": p.description,
                "Aliases": p.aliases,
                "Hide": p.hidden,
                "Overview": {"Tier": p.tier, "Features": p.features},
                "Options": [
                    {
                        "Name": o.name,
                        "Type": o.type,
                        "Required": o.required,
                        "IsPassword": o.password,
                        "Advanced": o.advanced,
                        "DefaultStr": o.default,
                        "Help": o.help,
                        "Examples": o.examples,
                    }
                    for o in p.options
                ],
            }
            for p in providers
        ],
    }
    try:
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".providers-", dir=str(settings.data_dir))
        with os.fdopen(fd, "w") as fh:
            json.dump(payload, fh)
        os.replace(tmp, cache_path())
    except OSError:
        pass


def _unlink(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


async def _rclone_version() -> str:
    """First line of `rclone version` (recorded in the cache for the UI)."""
    fd, conf = tempfile.mkstemp(prefix="rclone-", suffix=".conf", dir="/tmp")
    os.close(fd)
    try:
        proc = await asyncio.create_subprocess_exec(
            settings.rclone_bin,
            "--config",
            conf,
            "version",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
    except (OSError, asyncio.TimeoutError):
        return ""
    finally:
        _unlink(conf)
    lines = out.decode(errors="replace").splitlines()
    return lines[0].strip() if lines else ""


async def _fetch_raw() -> list:
    """Run `rclone config providers` and return the parsed JSON list."""
    fd, conf = tempfile.mkstemp(prefix="rclone-", suffix=".conf", dir="/tmp")
    os.close(fd)
    try:
        proc = await asyncio.create_subprocess_exec(
            settings.rclone_bin,
            "--config",
            conf,
            "config",
            "providers",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
    except (OSError, asyncio.TimeoutError):
        return []
    finally:
        _unlink(conf)
    if proc.returncode != 0:
        return []
    try:
        data = json.loads(stdout.decode(errors="replace"))
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else []


# ---------------------------------------------------------------- public API
_catalogue: list = []
_loaded = False
_lock = asyncio.Lock()


async def load_catalogue(force_refresh: bool = False) -> list:
    """The rclone backend catalogue, cached in memory and on disk."""
    global _catalogue, _loaded
    if _loaded and not force_refresh:
        return _catalogue
    async with _lock:
        if _loaded and not force_refresh:
            return _catalogue
        if not force_refresh:
            cached = _read_cache()
            if cached is not None:
                _catalogue, _loaded = cached[0], True
                return _catalogue
        raw = await _fetch_raw()
        if raw:
            _catalogue = _parse(raw)
            _loaded = True
            _write_cache(_catalogue, await _rclone_version())
            return _catalogue
        # rclone unavailable: fall back to a stale cache rather than an empty UI.
        cached = _read_cache()
        if cached is not None:
            _catalogue = cached[0]
        _loaded = True
        return _catalogue


def providers_cached() -> list:
    """Catalogue if loaded this process, else [] (never raises)."""
    return list(_catalogue)


def get_provider(name: str):
    """Provider by backend name (aliases accepted), or None."""
    wanted = (name or "").strip()
    if not wanted:
        return None
    for provider in _catalogue:
        if provider.name == wanted or wanted in provider.aliases:
            return provider
    return None


def is_known_backend(name: str) -> bool:
    return get_provider(name) is not None


def secret_option_names(backend: str) -> set:
    """Option names whose values must be encrypted at rest for `backend`."""
    provider = get_provider(backend)
    names = set(SECRET_NAMES)
    if provider is not None:
        names.update(o.name for o in provider.options if o.is_secret)
    return names


def bucket_based(backend: str) -> bool:
    """True when the backend exposes named containers (buckets)."""
    provider = get_provider(backend)
    return bool(provider and provider.bucket_based)


def reset_cache() -> None:
    """Forget the in-process catalogue (tests / after a rclone upgrade)."""
    global _catalogue, _loaded
    _catalogue, _loaded = [], False
