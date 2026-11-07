"""Bounded directory snapshots and native object operations; never recursive writes."""

import hashlib
import time

from .errors import MediaError
from .repository import canonical


async def snapshot(provider, root, binding, *, share=None, limit=1000):
    from .library import filename

    if root.get("type") != "folder":
        raise MediaError("source_not_a_folder")
    entries, seen = [], {root["file_id"]}
    pending = [(root["file_id"], "", 0)]
    while pending:
        folder, path, depth = pending.pop()
        if depth >= 16:
            raise MediaError("directory_depth_limit")
        marker, markers = "", set()
        while True:
            page = await provider.call(
                "inspect" if share is not None else "list",
                {
                    **(share if share is not None else binding),
                    **({"parent_file_id": folder} if share is not None else {"folder_id": folder}),
                    "marker": marker,
                },
            )
            for row in page["items"]:
                ident = row.get("file_id")
                if not isinstance(ident, str) or not ident or ident in seen:
                    raise MediaError("directory_identity_conflict")
                if row.get("parent_file_id") != folder or row.get("type") not in {"file", "folder"}:
                    raise MediaError("directory_identity_conflict")
                seen.add(ident)
                relative = "/".join(p for p in (path, filename(row["name"])) if p)
                if any(entry["path"] == relative for entry in entries):
                    raise MediaError("directory_identity_conflict")
                if row["type"] == "file" and (type(row.get("size")) is not int or row["size"] < 0):
                    raise MediaError("directory_content_unverifiable")
                entries.append(
                    {
                        **{
                            k: row.get(k)
                            for k in (
                                "file_id",
                                "parent_file_id",
                                "name",
                                "type",
                                "size",
                                "content_hash",
                                "content_hash_name",
                            )
                        },
                        "path": relative,
                    }
                )
                if len(entries) > limit:
                    raise MediaError("directory_entry_limit")
                if row["type"] == "folder":
                    pending.append((ident, relative, depth + 1))
            marker = page.get("marker")
            if not marker:
                break
            if marker in markers or len(markers) >= 100:
                raise MediaError("provider_incomplete_listing")
            markers.add(marker)
    entries.sort(key=lambda row: row["path"])
    return {
        "entries": entries,
        "count": len(entries),
        "digest": hashlib.sha256(canonical(entries).encode()).hexdigest(),
    }


def verify_copy(expected, actual):
    first = {row["path"]: row for row in expected["entries"]}
    second = {row["path"]: row for row in actual["entries"]}
    if first.keys() != second.keys():
        raise MediaError("directory_copy_unconfirmed", uncertain=True)
    for path, before in first.items():
        after = second[path]
        if after["type"] != before["type"] or before["type"] == "file" and after["size"] != before["size"]:
            raise MediaError("directory_copy_unconfirmed", uncertain=True)
        if before.get("content_hash") and (before["content_hash"], before.get("content_hash_name")) != (
            after.get("content_hash"),
            after.get("content_hash_name"),
        ):
            raise MediaError("directory_copy_unconfirmed", uncertain=True)
    return {
        "basis": "provider_directory_identity_and_manifest",
        "entries": len(first),
        "observed_at": time.time(),
    }
