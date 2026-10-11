"""Which buckets to offer for a remote, and why that list may be short.

Three call sites need this (transfer console, job form, restore) and they had
each grown their own copy, which is how they drifted apart: only one of them
handled a remote whose key cannot list buckets.

Two cases make listing impossible without the remote being broken:

* a **bucket-scoped Cloudflare R2 token** — ListBuckets returns 403 by design;
* **Garage with a key that cannot list** — the same shape of refusal.

In both, the remote's own saved bucket is the answer, so offer it rather than an
empty, alarming list.
"""

from app.rclone import _remote_row, list_buckets


async def bucket_choices(remote: str) -> tuple[list[str], str, str]:
    """Return `(buckets, note, error)` for a remote's bucket picker.

    `note` is non-empty when the list came from somewhere other than a real
    ListBuckets call, so the UI can explain itself instead of showing
    "Failed to load buckets" for a perfectly healthy remote.
    """
    remote = (remote or "").strip()
    if not remote:
        return [], "Choose a remote first.", ""

    row = await _remote_row(remote)
    saved = (getattr(row, "bucket", "") or "").strip() if row else ""

    try:
        buckets = await list_buckets(remote)
    except Exception:  # noqa: BLE001 - any failure is "cannot enumerate"
        if saved:
            return (
                [saved],
                f"This key cannot list buckets, so only '{saved}' — the bucket "
                f"saved with this remote — is offered. That is normal for a "
                f"bucket-scoped Cloudflare R2 token.",
                "",
            )
        return (
            [],
            "This key cannot list buckets, and this remote has no bucket saved, "
            "so there is nothing to choose from.",
            "cannot list buckets",
        )

    if not buckets and saved:
        return [saved], f"No buckets are visible; using the saved bucket '{saved}'.", ""
    return buckets, "", ""
