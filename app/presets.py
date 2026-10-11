"""Per-backend "simple" field sets and per-provider presets.

rclone's catalogue is excellent but overwhelming: the S3 backend alone declares
78 options, and the provider enum has 53 entries. Almost nobody configures more
than a handful, so the remote form shows:

* the **essential fields first** — for an S3-compatible backend that is exactly
  the four things everyone needs (endpoint, region, access key, secret key) plus
  the provider, and for every other backend the options rclone marks required
  plus a few obvious companions;
* everything else behind a searchable **Advanced options** panel;
* **presets** so choosing a provider fills in the values that provider needs
  (Cloudflare R2 → region `auto` and the R2 endpoint shape, AWS → no endpoint,
  Garage → region `garage`), while still allowing a custom region.

Presets are a convenience, never a restriction: any field can be overridden.
"""

# The four fields that configure an S3-compatible node, in the order a user
# fills them in. `provider` comes first because it decides the defaults.
S3_SIMPLE = ["provider", "endpoint", "region", "access_key_id", "secret_access_key"]

# How many options to show before the rest are pushed into "advanced".
SIMPLE_LIMIT = 6

# rclone's S3 backend declares ONE set of shared options for all 53 providers,
# so its help text says "AWS Access Key ID" / "AWS Secret Access Key" no matter
# which provider is selected. That is actively misleading for Cloudflare R2,
# where the credentials are an R2 API token and calling them "AWS" sends people
# looking in the wrong place entirely. These overrides replace the first line of
# the help for the shared S3 options when a provider is chosen. Providers not
# listed here keep rclone's own wording.
S3_OPTION_LABELS: dict[str, dict[str, str]] = {
    "AWS": {
        "access_key_id": "AWS Access Key ID.",
        "secret_access_key": "AWS Secret Access Key.",
    },
    "Cloudflare": {
        "access_key_id": "R2 Access Key ID (from the R2 API token).",
        "secret_access_key": "R2 Secret Access Key (from the R2 API token).",
    },
    "Other": {
        "access_key_id": "Access Key ID for this S3-compatible server.",
        "secret_access_key": "Secret Access Key for this S3-compatible server.",
    },
}


def option_labels(backend: str, provider: str) -> dict:
    """Help-text overrides for a provider's shared options (see S3_OPTION_LABELS)."""
    if backend != "s3":
        return {}
    table = S3_OPTION_LABELS
    name = (provider or "").strip()
    if name in table:
        return dict(table[name])
    for known, value in table.items():
        if known.lower() == name.lower():
            return dict(value)
    return dict(table["Other"])


# The region is a dropdown, but not every S3-compatible server uses an AWS
# region name — Garage wants "garage", R2 wants "auto", MinIO and Ceph accept
# almost anything. So the list is the full set of real AWS regions (taken from
# rclone, not hardcoded) with the provider's own value promoted to the top, and
# free text is still accepted for the long tail of self-hosted setups.
_COMMON_REGIONS = (
    "us-east-1", "us-west-2", "eu-west-1", "eu-central-1", "eu-north-1",
    "ap-southeast-1", "ap-northeast-1", "sa-east-1",
)


def region_choices(provider_spec, provider: str = "") -> list:
    """Region values to offer, provider's own first, then the AWS regions.

    Returned as [{value, help}] so it can drive the same combo box as the
    provider list. A duplicate of the provider default is never offered twice.
    """
    if provider_spec is None:
        return []
    regions: list = []
    seen: set = set()

    def add(value, help_text=""):
        text = (value or "").strip()
        if not text or text in seen:
            return
        seen.add(text)
        regions.append({"value": text, "help": help_text})

    if provider_spec.name == "s3":
        preset = preset_for(provider)
        add(preset.get("region", ""), "Default for this provider")
        for name in _COMMON_REGIONS:
            add(name)
        # rclone lists 128 real regions; add the rest so nothing is missing.
        option = next((o for o in provider_spec.options if o.name == "region"), None)
        if option is not None:
            for example in option.examples or []:
                add(example.get("value") if isinstance(example, dict) else example)
    else:
        option = next((o for o in provider_spec.options if o.name == "region"), None)
        if option is not None:
            for example in option.examples or []:
                add(example.get("value") if isinstance(example, dict) else example)
    return regions


# provider -> {region, endpoint, endpoint_placeholder, endpoint_required, note}
# Keys must be names rclone actually knows (`rclone config providers` → the
# s3 `provider` enum): an unknown value only produces
# 's3 provider "…" not known - please set correctly' and then behaves generically.
S3_PROVIDER_PRESETS: dict[str, dict] = {
    "AWS": {
        "region": "us-east-1",
        "endpoint": "",
        "endpoint_placeholder": "leave blank for AWS S3",
        "endpoint_required": False,
        "note": "Real AWS S3: leave the endpoint blank. The region must match "
        "the bucket's region (us-east-1, eu-central-1, …).",
        "hide": [
            "ibm_api_key",
            "ibm_resource_instance_id",
            "location_constraint",
            "bucket_object_lock_enabled",
        ],
    },
    "Cloudflare": {
        "region": "auto",
        "endpoint": "",
        "endpoint_placeholder": "https://<account-id>.r2.cloudflarestorage.com",
        "endpoint_required": True,
        "note": "Cloudflare R2: the endpoint is https://<ACCOUNT_ID>"
        ".r2.cloudflarestorage.com and the region is 'auto'. The Access Key "
        "ID / Secret are the R2 API token's keys. R2 ignores object ACLs "
        "and storage classes, so those options are hidden.",
        # R2 ignores object ACLs, storage classes and SSE, and has no IBM or
        # object-lock concepts. `env_auth` is hidden because R2 has no ambient
        # credential chain — the keys must be entered explicitly.
        "hide": ["acl", "storage_class", "server_side_encryption",
                 "sse_kms_key_id", "env_auth", "location_constraint",
                 "ibm_api_key", "ibm_resource_instance_id",
                 "bucket_object_lock_enabled", "no_check_bucket"],
    },
    "Minio": {
        "region": "us-east-1",
        "endpoint": "",
        "endpoint_placeholder": "http://minio.example.com:9000",
        "endpoint_required": True,
        "note": "MinIO: endpoint is the MinIO server URL (host:9000).",
        "hide": [
            "ibm_api_key",
            "ibm_resource_instance_id",
            "location_constraint",
            "bucket_object_lock_enabled",
        ],
    },
    "Other": {
        "region": "",
        "endpoint": "",
        "endpoint_placeholder": "http://192.168.1.10:3900  (or any https:// URL)",
        "endpoint_required": True,
        "note": "Any S3-compatible server on your own network — Garage, Ceph "
        "RGW, SeaweedFS, a NAS. rclone has no named provider for these, so pick "
        "'Other', give the endpoint as IP:port or a URL, and use the region the "
        "server reports (Garage accepts 'garage' or blank). Use the S3 API port, "
        "not the web UI: for Garage that is 3900 (the UI is 3903/3909).",
        "hide": [
            "ibm_api_key",
            "ibm_resource_instance_id",
            "location_constraint",
            "bucket_object_lock_enabled",
        ],
    },
    "Wasabi": {
        "region": "us-east-1",
        "endpoint": "https://s3.wasabisys.com",
        "endpoint_placeholder": "https://s3.wasabisys.com",
        "endpoint_required": False,
        "note": "Wasabi: the endpoint is fixed; pick the region closest to the "
        "bucket.",
        "hide": [
            "ibm_api_key",
            "ibm_resource_instance_id",
            "location_constraint",
            "bucket_object_lock_enabled",
        ],
    },
    "DigitalOcean": {
        "region": "us-east-1",
        "endpoint": "",
        "endpoint_placeholder": "https://<space-region>.digitaloceanspaces.com",
        "endpoint_required": True,
        "note": "DigitalOcean Spaces: both endpoint and region come from the "
        "space (e.g. nyc3 → https://nyc3.digitaloceanspaces.com).",
        "hide": [
            "ibm_api_key",
            "ibm_resource_instance_id",
            "location_constraint",
            "bucket_object_lock_enabled",
        ],
    },
    "Ceph": {
        "region": "",
        "endpoint": "",
        "endpoint_placeholder": "https://ceph.example.com:7480",
        "endpoint_required": True,
        "note": "Ceph RADOS Gateway: the endpoint is the RGW URL.",
        "hide": [
            "ibm_api_key",
            "ibm_resource_instance_id",
            "location_constraint",
            "bucket_object_lock_enabled",
        ],
    },
}

# Curated providers that rclone's enum does not know about. We keep the hints
# (they are what a user of that software needs) but never send the value as the
# rclone `provider` option, because rclone only warns and then guesses.
S3_PROVIDER_ALIASES = {
    "Backblaze": {
        "maps_to": "Other",
        "region": "us-west-002",
        "endpoint": "https://s3.us-west-002.backblazeb2.com",
        "endpoint_placeholder": "https://s3.<region>.backblazeb2.com",
        "endpoint_required": False,
        "note": "Backblaze B2 through its S3 API: pick provider 'Other' and put "
        "the endpoint below (it contains the region, e.g. us-west-002).",
        "hide": [
            "ibm_api_key",
            "ibm_resource_instance_id",
            "location_constraint",
            "bucket_object_lock_enabled",
        ],
    },
    "Garage": {
        "maps_to": "Other",
        "region": "garage",
        "endpoint": "",
        "endpoint_placeholder": "http://192.168.1.10:3900  (Garage API port)",
        "endpoint_required": True,
        "note": "Garage: rclone has no 'Garage' provider — leave the provider "
        "on 'Other' (or 'Minio'), put the Garage API endpoint here "
        "(http://<host>:3900) and keep the region as 'garage'.",
    },
}

# Fallback shown when the backend is not S3-compatible: the options rclone marks
# required come first, then its remaining ordinary options up to the limit.
_GENERIC_NOTE = ""


def preset_for(provider: str) -> dict:
    """Preset (region/endpoint defaults + note) for an S3 provider name.

    Falls back case-insensitively, then to the curated alias table (Garage…),
    then to the generic "Other" preset.
    """
    name = (provider or "").strip()
    for table in (S3_PROVIDER_PRESETS, S3_PROVIDER_ALIASES):
        if name in table:
            preset = dict(table[name])
            preset.pop("maps_to", None)
            return preset
        for known, value in table.items():
            if known.lower() == name.lower():
                preset = dict(value)
                preset.pop("maps_to", None)
                return preset
    return dict(S3_PROVIDER_PRESETS["Other"])


def provider_options(backend: str, provider: str) -> list:
    """Option names to *hide* for a provider — the options it does not use.

    rclone declares every option for every S3-compatible provider, so without
    this an R2 user is offered ACLs, storage classes and IBM keys that Cloudflare
    silently ignores. Hiding what a provider does not support keeps the form
    honest about what the endpoint will actually honour.
    """
    if backend != "s3":
        return []
    return list(preset_for(provider).get("hide", []))


def simple_options(provider_spec) -> list:
    """Ordered names of the options shown before "Advanced options".

    `provider_spec` is an `app.providers.Provider`. For S3-compatible backends
    that is the four essentials plus the provider; for anything else it is
    rclone's own required options followed by its ordinary ones.
    """
    if provider_spec is None:
        return []
    names = [o.name for o in provider_spec.options]
    if provider_spec.name == "s3":
        return [n for n in S3_SIMPLE if n in names]

    required = [o.name for o in provider_spec.options if o.required]
    simple = list(required)
    for option in provider_spec.options:
        if len(simple) >= SIMPLE_LIMIT:
            break
        if option.required or option.advanced or option.name in simple:
            continue
        simple.append(option.name)
    return simple


def visible_options(provider_spec, provider: str = "") -> list:
    """All option names that apply to this provider, in catalogue order."""
    if provider_spec is None:
        return []
    hidden = set(provider_options(provider_spec.name, provider))
    return [o.name for o in provider_spec.options if o.name not in hidden]


def presets_payload(provider_spec) -> dict:
    """Everything the remote form needs about defaults, for one backend."""
    if provider_spec is None:
        return {"presets": {}, "simple": [], "labels": {}, "regions": {}, "note": ""}
    simple = simple_options(provider_spec)
    payload = {
        "presets": {}, "simple": simple, "labels": {}, "regions": {}, "note": "",
    }
    if provider_spec.name == "s3":
        # Only the providers we have curated defaults for, to keep the payload
        # small; anything else falls back to the "Other" preset client-side.
        payload["presets"] = {
            **{k: dict(v) for k, v in S3_PROVIDER_PRESETS.items()},
            **{k: dict(v) for k, v in S3_PROVIDER_ALIASES.items()},
        }
        # Per-provider help text, so the credential fields stop saying "AWS"
        # when the provider is Cloudflare R2 or a self-hosted S3 clone.
        payload["labels"] = {
            k: dict(v) for k, v in S3_OPTION_LABELS.items()
        }
        # The region dropdown is provider-aware: R2 gets "auto" first, Garage
        # gets "garage" first, AWS gets the real region list.
        payload["regions"] = {
            k: region_choices(provider_spec, k) for k in payload["presets"]
        }
        payload["regions"][""] = region_choices(provider_spec, "")
    return payload