"""Provider-neutral cloud evidence; presentation paths never identify a cloud write."""

import time

from .errors import MediaError


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
