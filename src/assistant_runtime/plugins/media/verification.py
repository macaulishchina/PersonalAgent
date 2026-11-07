"""Provider-neutral cloud evidence; presentation paths never identify a cloud write."""

import time
from datetime import datetime

from .errors import MediaError


def recycle_evidence(before, actual, *, parent_scope="original", submitted_at=None):
    """Recycle placement differs from original ancestry; identity/content never do."""
    fields = ["file_id", "name", "type"]
    if before.get("type") == "file":
        fields.append("size")
    fresh_tombstone = False
    if parent_scope == "original" or (
        parent_scope == "recycle" and actual.get("parent_file_id") == before.get("parent_file_id")
    ):
        fields.append("parent_file_id")
    elif parent_scope == "recycle":
        # A changed recycle placement alone is insufficient. Require the provider's
        # tombstone time to bind this exact object to the persisted submission.
        try:
            stamp = datetime.fromisoformat(actual["trashed_at"].replace("Z", "+00:00"))
            if (
                stamp.tzinfo is None
                or submitted_at is None
                or not (submitted_at - 5 <= stamp.timestamp() <= time.time() + 300)
            ):
                raise ValueError("Unbound recycle observation")
        except (KeyError, TypeError, ValueError, AttributeError):
            raise MediaError("library_write_unconfirmed", uncertain=True, field="trashed_at") from None
        fresh_tombstone = True
    else:
        raise MediaError("provider_invalid_response", uncertain=True, field="parent_scope")
    evidence = {
        "parent_scope": parent_scope,
        "expected": {key: before.get(key) for key in fields},
        "observed": {key: actual.get(key) for key in fields},
    }
    for field in fields:
        if before.get(field) is None or actual.get(field) is None:
            raise MediaError("library_write_unconfirmed", uncertain=True, field=field, evidence=evidence)
        if actual[field] != before[field]:
            raise MediaError("library_content_changed", uncertain=True, field=field, evidence=evidence)
    if before.get("content_hash") and actual.get("content_hash"):
        for field in ("content_hash", "content_hash_name"):
            if actual.get(field) != before.get(field):
                raise MediaError("library_content_changed", uncertain=True, field=field)
            fields.append(field)
    return {
        "basis": "recycle_identity",
        "fields": fields,
        "parent_scope": parent_scope,
        "fresh_tombstone": fresh_tombstone,
        "observed_at": time.time(),
    }


def cloud_binding(target):
    return {key: target[key] for key in ("account", "drive_id", "folder_id") if key in target}


async def readback(plugin, item, file_id, method, *, before_rename=False):
    intent, expected = item["data"]["intent"], item["data"]["file"]
    destination = intent["destination"]
    if cloud_binding(plugin.target(intent["target"])) != cloud_binding(
        intent.get("root_destination", destination)
    ):
        raise MediaError("target_configuration_changed")
    actual = await plugin.provider.call("file", {**destination, "file_id": file_id})
    names = {intent.get("desired_name", expected["name"])}
    if before_rename:
        names.add(expected["name"])
    if (
        actual.get("file_id") != file_id
        or actual.get("parent_file_id") != destination["folder_id"]
        or actual.get("type") != "file"
        or actual.get("name") not in names
        or type(actual.get("size")) is not int
        or actual["size"] != expected["size"]
        or actual["size"] <= 0
    ):
        raise MediaError("cloud_file_verification_mismatch", uncertain=True)
    if expected.get("content_hash") and (
        actual.get("content_hash") != expected["content_hash"]
        or actual.get("content_hash_name") != expected.get("content_hash_name")
    ):
        raise MediaError("cloud_file_checksum_mismatch", uncertain=True)
    if method not in {"content_hash", "provider_file_id"}:
        raise MediaError("cloud_identity_unconfirmed", uncertain=True)
    return actual, {
        "state": "verified",
        "observed_at": time.time(),
        "target": intent["target"],
        **cloud_binding(destination),
        "file_id": actual["file_id"],
        "name": actual["name"],
        "size": actual["size"],
        "identity_evidence": method,
        "checks": ["file_id", "parent_file_id", "name", "size", "type"]
        + (["content_hash"] if expected.get("content_hash") else []),
    }
