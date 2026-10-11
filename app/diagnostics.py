"""Turning an rclone failure into something you can act on.

A raw rclone/S3 error is a wall of text:

    error listing: operation error S3: ListBuckets, exceeded maximum number of
    attempts, 1, https response error StatusCode: 403, …

This module turns that into one short line plus a concrete next step, and it
also checks the configuration *before* calling rclone, because the most common
S3-compatible mistakes (Cloudflare R2 without the account ID in the endpoint, a
bucket pasted into the endpoint, a wrong region) are obvious from the settings
and far cheaper to spot than from an HTTP error.

It also recognises one case that is **not** a failure: a token that can use
objects but may not *list* buckets. Cloudflare R2 API tokens are bucket-scoped
by design, so `ListBuckets` returns 403 even though backups work perfectly —
that remote must not be rejected.
"""

import re

# Cloudflare R2 endpoints are https://<ACCOUNT_ID>.r2.cloudflarestorage.com
_CF_HOST_RE = re.compile(
    r"^https?://(?P<host>[^/:]+)\.r2\.cloudflarestorage\.com(?P<path>/.*)?$", re.I
)
_CF_ACCOUNT_RE = re.compile(r"^[0-9a-f]{32}$", re.I)


def _truthy(value) -> bool:
    return str(value or "").strip().lower() in ("true", "1", "yes", "on")


def s3_provider_values() -> set:
    """The provider names rclone accepts for the S3 backend, exact-case.

    rclone matches this enum case-sensitively — 'cloudflare' is rejected with
    's3 provider "cloudflare" not known' even though 'Cloudflare' is valid.
    """
    try:
        from app.providers import _read_cache, get_provider

        s3 = get_provider("s3")
        if s3 is None:
            # The in-memory catalogue may not be loaded (a test, or a request
            # that arrives before startup finishes). The on-disk cache is the
            # same data and is what the form itself was rendered from.
            cached = _read_cache()
            s3 = next(
                (p for p in (cached[0] if cached else []) if p.name == "s3"), None
            )
        if s3 is None:
            return set()
        option = next((o for o in s3.options if o.name == "provider"), None)
        if option is None:
            return set()
        # Examples are plain {"value": …, "help": …} dicts.
        return {
            e.get("value")
            for e in option.examples
            if isinstance(e, dict) and e.get("value")
        }
    except Exception:  # catalogue unavailable: never block the user on this
        return set()


def canonical_s3_provider(value: str) -> str:
    """The correctly-cased provider name, or "" when the value is unknown."""
    text = (value or "").strip()
    if not text:
        return ""
    allowed = s3_provider_values()
    if not allowed:
        return ""
    if text in allowed:
        return text
    lowered = text.lower()
    for known in allowed:
        if known.lower() == lowered:
            return known
    return ""


def _not_a_key(value: str) -> bool:
    """True when a credential field clearly holds a token or a whole line, not a key.

    Deliberately structural rather than length-based: a MinIO/Ceph username can be
    any string, so only markers that cannot appear in a key are treated as proof.
    """
    if not value:
        return False
    if len(value) > 200 or any(ch.isspace() for ch in value):
        return True
    if value.startswith("eyJ") and value.count(".") == 2:  # JWT
        return True
    lowered = value.lower()
    return any(marker in lowered for marker in ("bearer ", "authorization", "://"))


def preflight(backend: str, options: dict) -> str:
    """A configuration problem that would certainly fail, or "" if it looks sane.

    Returns one readable line so the caller can show it next to the test result.
    """
    if backend != "s3":
        return ""
    provider = (options.get("provider") or "").strip()
    endpoint = (options.get("endpoint") or "").strip()
    region = (options.get("region") or "").strip()
    access = (options.get("access_key_id") or "").strip()
    secret = (options.get("secret_access_key") or "").strip()

    # rclone matches the provider enum case-sensitively, so 'cloudflare' fails
    # with 'provider "cloudflare" not known' however good the rest is. Say so,
    # and name the exact spelling to use.
    canonical = canonical_s3_provider(provider)
    if provider and canonical:
        # Only judge the spelling when the enum is actually known. If the
        # catalogue is unavailable, an empty result must not be mistaken for
        # "this value is wrong" — that produced the nonsense
        # "needs the provider spelled ''" message.
        if canonical != provider:
            return (
                f"rclone needs the provider spelled '{canonical}', not '{provider}' "
                f"— the name is case-sensitive. Change it to '{canonical}'."
            )
    elif provider:
        allowed = s3_provider_values()
        if allowed:
            near = [k for k in allowed if k.lower().startswith(provider.lower()[:4])]
            hint = f" Did you mean '{near[0]}'?" if len(near) == 1 else ""
            return (
                f"'{provider}' is not an S3 provider rclone knows — the name is "
                f"case-sensitive, so it must match exactly.{hint} Pick one from the "
                f"provider list, or leave it blank for the default."
            )

    if provider.lower() == "cloudflare":
        if not endpoint:
            return (
                "Cloudflare R2 needs an endpoint: "
                "https://<ACCOUNT_ID>.r2.cloudflarestorage.com"
            )
        match = _CF_HOST_RE.match(endpoint)
        if not match:
            return (
                "The R2 endpoint must look like "
                "https://<ACCOUNT_ID>.r2.cloudflarestorage.com "
                "(32-character account ID from the R2 dashboard, not the bucket name)"
            )
        if not _CF_ACCOUNT_RE.match(match.group("host")):
            return (
                f"'{match.group('host')}' is not a Cloudflare account ID — the "
                "endpoint needs the 32-character account ID, not the bucket name"
            )
        if match.group("path"):
            return (
                "Don't put the bucket in the endpoint: use "
                "https://<ACCOUNT_ID>.r2.cloudflarestorage.com and pick the bucket "
                "separately (in Jobs or Restore)"
            )
        if region and region.lower() != "auto":
            return (
                f"Cloudflare R2 requires the region 'auto' (you set '{region}')"
            )
    elif provider.lower() not in ("aws", "", "amazon") and not endpoint:
        return (
            f"provider '{provider}' needs an endpoint — either its own URL or, "
            "for AWS, leave the endpoint blank"
        )

    if access and _not_a_key(access):
        return (
            "The access key ID doesn't look like a key — paste only the key itself, "
            "not a token, a curl command or a whole config line."
        )
    if secret and _not_a_key(secret):
        return (
            "The secret key doesn't look like a key — paste only the secret, not the "
            "whole line it appears on."
        )
    if _truthy(options.get("env_auth")) and not (access and secret):
        return (
            "env_auth is on, so rclone reads the keys from "
            "RCLONE_S3_ACCESS_KEY_ID / RCLONE_S3_SECRET_ACCESS_KEY instead of "
            "the fields above"
        )
    if not access and not secret:
        return "No access key / secret key entered."
    if access and not secret:
        return "An access key was entered but the secret key is missing."
    return ""


# A body that rclone could not parse as S3 XML: the endpoint is not an S3 API.
_NOT_S3_RE = re.compile(
    r"deserialization failed|XML syntax error|failed to decode response body", re.I
)


# Order matters: the first match wins.
_ERROR_RULES = [
    (
        # Checked first: if the body could not be parsed as S3 XML then the
        # endpoint is not an S3 API at all, whatever the status code was. This
        # is the classic "pointed at the admin UI / reverse proxy" mistake — e.g.
        # Garage's UI on :3909 returns HTML 200, so rclone reports a confusing
        # "XML syntax error" instead of "wrong port".
        "not s3",
        _NOT_S3_RE,
        "That URL answered with a web page, not the S3 API — it is an admin/UI "
        "port, not the storage port. Use the S3 API port (for Garage that is 3900; "
        "the Garage UI is 3903/3909 and the admin API is 3903).",
    ),
    (
        "provider not known",
        re.compile(r'provider "([^"]+)" not known', re.I),
        "rclone does not know that provider value",
    ),
    (
        "signature",
        re.compile(r"SignatureDoesNotMatch|signature.*(mismatch|does not match)", re.I),
        "The secret key does not match this access key",
    ),
    (
        "invalid key",
        re.compile(r"InvalidAccessKeyId|The AWS Access Key Id you provided", re.I),
        "The access key ID is not valid (check for a stray space or a truncated paste)",
    ),
    (
        "access denied",
        re.compile(r"AccessDenied|StatusCode: 403|Forbidden", re.I),
        "Access denied — the key/secret pair is wrong, or the token lacks permission",
    ),
    (
        "not found",
        re.compile(r"NoSuchBucket|StatusCode: 404|Not Found", re.I),
        "Not found — the endpoint is wrong (for R2 that is usually a missing or "
        "incorrect account ID)",
    ),
    (
        "dns",
        re.compile(r"no such host|server misbehaving", re.I),
        "The endpoint host could not be resolved — check the URL for typos",
    ),
    (
        "refused",
        re.compile(r"connection refused", re.I),
        "The connection was refused — nothing is listening on that host:port",
    ),
    (
        "tls",
        re.compile(r"tls: handshake failure|x509|certificate", re.I),
        "TLS failed — a proxy or firewall is intercepting the connection, or the "
        "certificate is not trusted",
    ),
    (
        "timeout",
        re.compile(r"context deadline exceeded|i/o timeout", re.I),
        "The endpoint did not answer in time",
    ),
]

_R2_EXTRA = (
    " For R2: the endpoint must contain your 32-character account ID, the region "
    "is 'auto', and the keys come from the R2 API token page (not the "
    "Cloudflare API token page)."
)


# Garage (and some S3 servers) report the expected key length, which pinpoints a
# bad paste far better than a generic 400: "has length 140, should be 32".
_KEY_LENGTH_RE = re.compile(
    r"access key has length (?P<got>\d+), should be (?P<want>\d+)", re.I
)


# Where the key ID actually lives, per provider. Telling a Cloudflare user to
# run "garage key list" is worse than saying nothing.
_KEY_SOURCE = {
    "cloudflare": (
        "R2's Access Key ID is the value shown next to the Secret Access Key on "
        "the Cloudflare dashboard (R2 → Manage R2 → Account API Tokens). Copy the "
        "two boxes separately; the secret is not part of the ID."
    ),
    "aws": (
        "The Access Key ID is the 20-character ID from AWS IAM → Access keys. "
        "It is not the Secret Access Key."
    ),
    "minio": "The Access Key is the user name (or root user) you created in MinIO.",
}
_GENERIC_KEY_SOURCE = (
    "Copy the key ID on its own — not the whole line, a token, or a curl command."
)


def _key_length_error(text: str, provider: str = "") -> str:
    match = _KEY_LENGTH_RE.search(text)
    if not match:
        return ""
    got, want = match.group("got"), match.group("want")
    where = _KEY_SOURCE.get(
        (provider or "").strip().lower(), _GENERIC_KEY_SOURCE
    )
    return (
        f"The access key ID is {got} characters but must be {want}, so that field "
        f"is holding something other than a key. {where}"
    )


def explain(error: str, provider: str = "") -> str:
    """One short, actionable line for an rclone/S3 error."""
    text = (error or "").strip()
    if not text:
        return "rclone failed without a message"
    length_error = _key_length_error(text, provider)
    if length_error:
        return length_error
    is_cloudflare = provider.strip().lower() == "cloudflare"
    for key, pattern, human in _ERROR_RULES:
        if pattern.search(text):
            if is_cloudflare and key in ("access denied", "not found", "invalid key"):
                return human + _R2_EXTRA
            return human
    return text.splitlines()[-1][:300]


def can_list_buckets(error: str) -> bool:
    """True when the failure is specifically "may not list buckets".

    A token scoped to one bucket can still read and write objects, so this must
    not be reported as a broken remote.
    """
    text = error or ""
    # A non-XML body means the endpoint was never an S3 API, so a 403 there is
    # the UI/proxy refusing us — not a bucket-scoped token. Never let this
    # pattern mark a broken remote as working.
    if _NOT_S3_RE.search(text):
        return False
    return bool(re.search(r"ListBuckets", text, re.I)) and bool(
        re.search(r"AccessDenied|StatusCode: 403|Forbidden", text, re.I)
    )


def list_bucket_notice() -> str:
    """Shown when the credentials work but bucket listing is not permitted."""
    return (
        "Connected, but this key cannot list buckets (Cloudflare R2 API tokens are "
        "scoped to one bucket, so listing buckets returns 403). The remote is "
        "usable — set the Bucket field to that bucket now, and it is saved with "
        "the remote for Jobs, Restore and the Transfer console. Without it there "
        "is nothing for those screens to list."
    )


def bucket_scoped_notice(bucket: str, entries: list) -> str:
    """Shown when a named bucket was proven reachable with a bucket-scoped token."""
    shown = ", ".join(entries[:5]) if entries else ""
    tail = f" It contains: {shown}." if shown else " It is currently empty."
    return (
        f"Bucket '{bucket}' is reachable. This key cannot list buckets (R2 API "
        f"tokens are scoped to a single bucket), which is why only the bucket you "
        f"named is shown.{tail} It is saved as this remote's bucket, so Jobs and "
        f"Restore will use it."
    )
