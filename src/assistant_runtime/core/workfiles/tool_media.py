"""Retain tool-returned images once; no remote fetching or tool invocation."""

import asyncio
import base64
import hashlib
import json
import os
import weakref
from assistant_runtime.api.tool_calls import ToolMediaReceipt
from assistant_runtime.api.system import system
from .media import MAX_BYTES

MIMES = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}


class ToolMedia:
    def __init__(self, files):
        self.files, self.db = files, files.db
        self.locks = weakref.WeakValueDictionary()
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS tool_media_receipts (execution_id TEXT NOT NULL, "
            "call_id TEXT NOT NULL, fingerprint TEXT NOT NULL, receipt TEXT NOT NULL, "
            "PRIMARY KEY(execution_id,call_id))"
        )
        self.db.commit()

    async def register(self, execution_id, call_id, images, area, *, owner):
        if not isinstance(images, list) or not 1 <= len(images) <= 4:
            raise ValueError("Expected one to four tool images")
        encoded = json.dumps(images, sort_keys=True, separators=(",", ":"))
        fingerprint = hashlib.sha256(encoded.encode()).hexdigest()
        key = (execution_id, call_id)
        lock = self.locks.setdefault(key, asyncio.Lock())
        async with lock:
            old = self.db.execute(
                "SELECT fingerprint,receipt FROM tool_media_receipts WHERE execution_id=? AND call_id=?", key
            ).fetchone()
            if old:
                if old[0] != fingerprint:
                    raise ValueError("Tool media receipt conflicts with the received result")
                receipt = json.loads(old[1])
                # Never claim readiness for a disappeared/corrupt retained object.
                for ref in receipt["attachments"]:
                    if self.files.media.image_record(ref["id"])["sha256"] != ref["sha256"]:
                        raise ValueError("Retained tool image integrity check failed")
                    directory = self.files.area(area) / "artifacts"
                    self.write(
                        directory,
                        ref["artifact_name"],
                        self.files.media.folder.joinpath(ref["id"]).read_bytes(),
                    )
                return receipt
            attachments, issues, seen = [], [], set()
            for index, image in enumerate(images):
                try:
                    if not isinstance(image, dict) or set(image) != {"data", "mime_type"}:
                        raise ValueError("Invalid image block")
                    encoded = image["data"]
                    mime = image["mime_type"]
                    if (
                        not isinstance(mime, str)
                        or mime not in MIMES
                        or not isinstance(encoded, str)
                        or len(encoded) > (MAX_BYTES + 2) // 3 * 4
                    ):
                        raise ValueError("Invalid image bounds or type")
                    data = base64.b64decode(encoded, validate=True)
                    digest = hashlib.sha256(data).hexdigest()
                    if (mime, digest) in seen:
                        continue
                    ref = await self.files.media.add_image(
                        data,
                        "工具图片 " + str(index + 1) + MIMES[mime],
                        key=f"tool:{execution_id}:{call_id}:{index}",
                        attached=True,
                        mime_type=mime,
                    )
                    if self.files.media.image_record(ref["id"])["sha256"] != digest:
                        raise ValueError("Retained tool image integrity check failed")
                    name = "tool-image-" + ref["id"] + MIMES[mime]
                    # Artifacts are the existing mutable working deliverables, not publications.
                    directory = self.files.area(area) / "artifacts"
                    pending = asyncio.create_task(asyncio.to_thread(self.write, directory, name, data))
                    try:
                        await asyncio.shield(pending)
                    except asyncio.CancelledError:
                        await asyncio.gather(pending, return_exceptions=True)
                        raise
                    attachments.append(
                        {
                            **ref,
                            "sha256": digest,
                            "bytes": len(data),
                            "reference": "attachment:" + ref["id"],
                            "path": str(directory / name),
                            "artifact_name": name,
                        }
                    )
                    seen.add((mime, digest))
                except (ValueError, OSError, ImportError) as error:
                    code = (
                        "tool_media_decoder_unavailable"
                        if isinstance(error, ImportError)
                        else "tool_media_storage_unavailable"
                        if isinstance(error, OSError)
                        else "tool_media_invalid_image"
                    )
                    issues.append({"index": index, "code": code})
            receipt: ToolMediaReceipt = {
                "state": "partial" if attachments and issues else "ready" if attachments else "failed",
                "attachments": attachments,
                "issues": issues,
            }
            self.files.inputs.grant(attachments, owner)
            self.files.inputs.grant(attachments, execution_id)
            with self.db:
                self.db.execute(
                    "INSERT INTO tool_media_receipts VALUES (?,?,?,?)",
                    (*key, fingerprint, json.dumps(receipt, ensure_ascii=False)),
                )
            return receipt

    @staticmethod
    def write(folder, name, data):
        directory = system.directory(folder)
        try:
            try:
                fd = directory.open_file(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
            except FileExistsError:
                with os.fdopen(directory.open_file(name, os.O_RDONLY), "rb") as source:
                    previous = source.read(MAX_BYTES + 1)
                if previous != data:
                    raise ValueError("Tool image artifact already contains different bytes")
                return
            try:
                with os.fdopen(fd, "wb") as target:
                    target.write(data)
                    target.flush()
                    os.fsync(target.fileno())
                directory.sync()
            except BaseException:
                directory.unlink(name)
                raise
        finally:
            directory.close()
