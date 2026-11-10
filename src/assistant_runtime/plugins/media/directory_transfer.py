"""One native folder copy with bounded source/target evidence and no write replay."""

import time

from .directories import snapshot, verify_copy
from .errors import MediaError


async def process(plugin, item, *, observe_only=False):
    repo, ident, data = plugin.repo, item["id"], item["data"]
    intent = data["intent"]
    destination = intent["destination"]
    if plugin.target(intent["target"]) != intent.get("root_destination", destination):
        raise MediaError("target_configuration_changed")
    if not data.get("submitted"):
        if observe_only:
            return  # No submission receipt: verification cannot initiate a copy.
        from .library import filename

        if data.get("cancel_requested"):
            repo.change(ident, "cancelled")
            return
        await plugin.library.destination(intent["target"], destination["folder_id"])
        root = await plugin.provider.call("source", {**intent, "account": destination["account"]})
        if root.get("type") != "folder" or root.get("file_id") != intent["file_id"]:
            raise MediaError("source_not_a_folder")
        filename(root["name"])
        await plugin.library.conflict(intent["target"], destination["folder_id"], root["name"])
        manifest = await snapshot(
            plugin.provider,
            root,
            destination,
            share={
                "account": destination["account"],
                "share_id": intent["share_id"],
                "password": intent["password"],
            },
        )
        needed = sum(row["size"] for row in manifest["entries"] if row["type"] == "file")
        capacity = await plugin.provider.call("capacity", {"account": destination["account"]})
        if any(type(capacity.get(key)) is not int for key in ("total", "used")):
            raise MediaError("capacity_unconfirmed")
        if capacity["total"] - capacity["used"] < needed:
            raise MediaError("capacity_exhausted")
        plugin.library.protect_destination(destination, {root["name"]}, excluding=ident)
        if not repo.begin_write(ident, submitted_at=time.time(), file=root, manifest=manifest):
            return
        # Persist submission intent before the only write. A lost reply never reaches this branch again.
        try:
            receipt = await plugin.provider.call("copy", {**intent, **destination})
        except MediaError as exc:
            if not exc.uncertain:
                repo.change(ident, "failed", submitted=False, error=exc.code)
                return
            raise
        repo.change(ident, "verifying", copy_receipt=receipt)
        data = repo.get(ident)["data"]
    receipt = data.get("copy_receipt", {})
    file_id = receipt.get("file_id")
    if not file_id and receipt.get("task_id"):
        status = await plugin.provider.call(
            "copy_status",
            {
                "account": destination["account"],
                "task_id": receipt["task_id"],
            },
        )
        file_id = status.get("file_id")
        if file_id:
            receipt = {**receipt, "file_id": file_id}
            repo.change(ident, "verifying", copy_receipt=receipt)
        elif time.time() - data["submitted_at"] < 600:
            repo.change(ident, "verifying", due=time.time() + 2)
            return
    if not file_id:
        repo.change(ident, "uncertain", error="directory_copy_receipt_unconfirmed")
        return
    actual = await plugin.provider.call("file", {**destination, "file_id": file_id})
    expected = data["file"]
    if (
        actual.get("type") != "folder"
        or actual.get("file_id") != file_id
        or actual.get("name") != expected["name"]
        or actual.get("parent_file_id") != destination["folder_id"]
    ):
        raise MediaError("directory_copy_unconfirmed", uncertain=True)
    manifest = await snapshot(plugin.provider, actual, destination)
    repo.change(
        ident,
        item["state"] if observe_only and item["state"] == "completed" else "verifying",
        destination_manifest=manifest,
    )
    evidence = verify_copy(data["manifest"], manifest)
    plugin.library.remember(intent["target"], actual, trashed=False)
    repo.change(
        ident,
        "completed",
        file=actual,
        saved_file_id=file_id,
        verification="provider_directory_manifest",
        cloud_verification=evidence,
        error=None,
    )


async def reconcile(plugin, item):
    """Read existing receipts only; a later missing object does not erase prior completion."""
    async with plugin.library.write_lock(item):
        current = plugin.repo.get(item["id"])
        try:
            await process(plugin, current, observe_only=True)
        except MediaError as exc:
            if current["state"] == "completed":
                plugin.repo.change(
                    current["id"],
                    "completed",
                    cloud_verification={
                        "state": "unconfirmed",
                        "error": exc.code,
                        "observed_at": time.time(),
                    },
                )
            else:
                plugin.repo.change(
                    current["id"],
                    "uncertain",
                    error=exc.code,
                    due=current["due"],
                    verification_issue={"code": exc.code, "field": exc.field},
                )
    return plugin.repo.public(plugin.repo.get(item["id"]))
