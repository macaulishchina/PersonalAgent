"""Bounded durable groups of existing media operations, with one summary notice."""

import asyncio
import hashlib
import json
import time
import uuid

from ...kernel.contracts import MediaRequest
from .repository import canonical, CURRENT_BATCH
from .errors import MediaError


class Batches:
    def __init__(self, plugin):
        self.p, self.db = plugin, plugin.repo.db
        self.db.execute("""CREATE TABLE IF NOT EXISTS media_batches (
            id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, owner TEXT NOT NULL,
            state TEXT NOT NULL, data TEXT NOT NULL)""")
        self.db.commit()

    def create(self, args, owner, caller):
        ident = args.get("request_id")
        try:
            if str(uuid.UUID(ident)) != ident:
                raise ValueError()
        except (ValueError, TypeError, AttributeError):
            raise ValueError("Batch requires canonical UUID") from None
        action = args.get("action")
        entries = args.get("items")
        target = args.get("target")
        binding = self.p.target(target)
        if (
            not isinstance(action, str)
            or action not in {"transfer", "download"}
            or not isinstance(entries, list)
            or not 1 <= len(entries) <= 100
        ):
            raise ValueError("Batch requires transfer/download and 1..100 explicit items")
        openlist_download = (
            action == "download" and self.p.settings.files_for(binding["account"]) == "openlist"
        )
        if not openlist_download:
            self.p.provider.require(binding["account"], "copy" if action == "transfer" else "download_url")
        fields = (
            ({"path"} if openlist_download else {"file_id"})
            if action == "download"
            else {
                "share_url",
                "password",
                "file_id",
                "folder_id",
                "name",
                "collection",
                "season",
                "episode",
                "version",
                "parent_file_id",
            }
        )
        for entry in entries:
            if (
                not isinstance(entry, dict)
                or set(entry) - fields
                or ("path" if openlist_download else "file_id") not in entry
            ):
                raise ValueError("Invalid batch item fields")
            if action == "transfer":
                from .search import share_provider

                self.p.provider.resolve(binding["account"], provider=share_provider(entry.get("share_url")))
                if entry.get("name") is not None:
                    self.p.provider.require(binding["account"], "rename")
        fingerprint = hashlib.sha256(canonical([owner, args]).encode()).hexdigest()
        row = self.db.execute("SELECT fingerprint FROM media_batches WHERE id=?", (ident,)).fetchone()
        if row:
            if row[0] != fingerprint:
                raise ValueError("Batch UUID conflicts with prior intent")
            return self.get(ident)
        if (
            self.db.execute("SELECT count(*) FROM media_batches").fetchone()[0]
            >= self.p.settings.max_operations
        ):
            raise ValueError("Batch capacity reached")
        if (
            self.db.execute(
                "SELECT count(*) FROM media_batches WHERE state NOT IN ('completed','partial','failed','cancelled')"
            ).fetchone()[0]
            >= self.p.settings.queue_capacity
        ):
            raise ValueError("Pending batch capacity reached")
        data = {
            "target": target,
            "action": action,
            "caller_run": caller,
            "created_at": time.time(),
            "binding": dict(self.p.target(target)),
            "file_backend": self.p.settings.files_for(binding["account"]),
            "file_endpoint": self.p.settings.openlist_url if openlist_download else None,
            "items": [{"arguments": e, "state": "pending"} for e in entries],
        }
        with self.db:
            self.db.execute(
                "INSERT INTO media_batches VALUES(?,?,?,?,?)",
                (ident, fingerprint, owner, "queued", canonical(data)),
            )
        return self.get(ident)

    def get(self, ident):
        row = self.db.execute("SELECT owner,state,data FROM media_batches WHERE id=?", (ident,)).fetchone()
        if not row:
            raise ValueError("Media batch not found")
        data = json.loads(row[2])
        return {
            "batch_id": ident,
            "owner": row[0],
            "state": row[1],
            "target": data["target"],
            "items": [{k: v for k, v in e.items() if k != "arguments"} for e in data["items"]],
        }

    def cancel(self, ident, owner):
        result = self.get(ident)
        if owner != "assistant" and result["owner"] != owner:
            raise ValueError("Batch belongs to another task")
        row = self.db.execute("SELECT data FROM media_batches WHERE id=?", (ident,)).fetchone()
        data = json.loads(row[0])
        data["cancel_requested"] = True
        with self.db:
            self.db.execute(
                "UPDATE media_batches SET state=?,data=? WHERE id=?", ("running", canonical(data), ident)
            )
        return self.get(ident)

    def save(self, ident, state, data):
        saved = json.loads(
            self.db.execute("SELECT data FROM media_batches WHERE id=?", (ident,)).fetchone()[0]
        )
        if saved.get("cancel_requested"):
            data["cancel_requested"] = True
        with self.db:
            self.db.execute(
                "UPDATE media_batches SET state=?,data=? WHERE id=?", (state, canonical(data), ident)
            )
            if state in {
                "completed",
                "partial",
                "failed",
                "cancelled",
                "uncertain",
            } and state not in data.get("notified_states", []):
                completed = sum(e["state"] == "completed" for e in data["items"])
                label = {
                    "completed": "完成",
                    "partial": "部分完成",
                    "failed": "失败",
                    "cancelled": "已取消",
                    "uncertain": "部分结果待核实",
                }[state]
                self.p.repo.enqueue(
                    "media-batch:" + ident + ":" + state,
                    "媒体批次结果",
                    f"{completed}/{len(data['items'])} 项完成。状态：{label}。批次：{ident}",
                )
                data["notified_states"] = [*data.get("notified_states", []), state]
                self.db.execute("UPDATE media_batches SET data=? WHERE id=?", (canonical(data), ident))

    async def tick(self, ident, owner, data):
        for index, entry in enumerate(data["items"]):
            if entry["state"] == "waiting_auth" and not entry.get("operation_id"):
                if self.p.repo.account(data["binding"]["account"])["state"] == "ready" or data.get(
                    "cancel_requested"
                ):
                    entry["state"] = "pending"
            if entry["state"] == "pending":
                if (
                    not data.get("cancel_requested")
                    and self.db.execute(
                        "SELECT count(*) FROM media_operations WHERE state NOT IN ('completed','failed','cancelled')"
                    ).fetchone()[0]
                    >= self.p.settings.queue_capacity
                ):
                    break  # Backpressure, not a failed media acquisition.
                if self.p.target(data["target"]) != data["binding"] or (
                    data["action"] == "download"
                    and self.p.settings.files_for(data["binding"]["account"])
                    != data.get("file_backend", "native")
                    or (
                        data.get("file_endpoint") is not None
                        and data["file_endpoint"] != self.p.settings.openlist_url
                    )
                ):
                    entry.update(state="failed", error="target_configuration_changed")
                    continue
                if data.get("cancel_requested"):
                    entry["state"] = "cancelled"
                else:
                    token = CURRENT_BATCH.set(ident)
                    try:
                        # Authorization was captured at submission. Future background
                        # execution expiry must not revoke this already accepted batch.
                        result = await self.p.request(
                            MediaRequest(
                                data["action"],
                                {
                                    **entry["arguments"],
                                    "target": data["target"],
                                    "request_id": str(uuid.uuid5(uuid.UUID(ident), str(index))),
                                },
                            )
                        )
                        entry.update(operation_id=result["operation_id"], state=result["state"])
                        item = self.p.repo.get(result["operation_id"])
                        if item["data"].get("batch_id") == ident:
                            self.p.repo.change(
                                item["id"], item["state"], owner=owner, caller_run=data["caller_run"]
                            )
                    except (ValueError, RuntimeError) as exc:
                        # Do not expose SDK strings or account details.
                        auth = isinstance(exc, MediaError) and exc.code == "auth_required"
                        entry.update(
                            state="waiting_auth" if auth else "failed",
                            error=exc.code if isinstance(exc, MediaError) else "invalid_batch_item",
                        )
                    finally:
                        CURRENT_BATCH.reset(token)
                self.save(ident, "running", data)
            if entry.get("operation_id"):
                operation = self.p.repo.get(entry["operation_id"])
                if data.get("cancel_requested") and operation["data"].get("batch_id") != ident:
                    if operation["state"] not in {"completed", "failed", "cancelled"}:
                        entry.update(state="cancelled", detached=True)
                        continue
                if (
                    data.get("cancel_requested")
                    and operation["data"].get("batch_id") == ident
                    and entry["state"] not in {"completed", "failed", "cancelled"}
                ):
                    await self.p.request(MediaRequest("cancel", {"operation_id": entry["operation_id"]}))
                entry["state"] = self.p.repo.get(entry["operation_id"])["state"]
        states = {e["state"] for e in data["items"]}
        if states == {"completed"}:
            state = "completed"
        elif states <= {"completed", "failed", "cancelled", "uncertain"}:
            state = (
                "uncertain"
                if "uncertain" in states
                else (
                    "partial"
                    if "completed" in states
                    else "cancelled"
                    if states == {"cancelled"}
                    else "failed"
                )
            )
        else:
            state = "waiting_auth" if "waiting_auth" in states else "running"
        self.save(ident, state, data)

    async def worker(self):
        cursor = ""
        while True:
            rows = self.db.execute(
                "SELECT id,owner,data FROM media_batches WHERE state IN ('queued','running','waiting_auth','uncertain') AND id>? ORDER BY id LIMIT 32",
                (cursor,),
            ).fetchall()
            cursor = rows[-1][0] if rows else ""
            for ident, owner, raw in rows:
                try:
                    await self.tick(ident, owner, json.loads(raw))
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # Preserve pending receipts; no implicit replay of provider writes.
                    self.db.execute("UPDATE media_batches SET state='uncertain' WHERE id=?", (ident,))
                    self.db.commit()
            await asyncio.sleep(2)
