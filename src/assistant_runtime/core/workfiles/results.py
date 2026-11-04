"""Owned received-result snapshots, not an execution ledger or an agent loop."""

import asyncio
import base64
import hashlib
import json
import os
import re
import sqlite3
import time
from contextlib import nullcontext

from assistant_runtime.api.results import ResultAccessError
from assistant_runtime.api.system import system
from .inputs import caller

CHUNK = 1024 * 1024
DEFAULT_BYTES = 512 * CHUNK
DEFAULT_OBJECT_BYTES = 64 * CHUNK


def settings(values):
    days = values.get("result_retention_days", 0)
    quota = values.get("result_storage_bytes", DEFAULT_BYTES)
    maximum = values.get("result_max_bytes", DEFAULT_OBJECT_BYTES)
    if type(days) is not int or days not in (0, 7, 30, 365):
        raise ValueError("result_retention_days must be 0, 7, 30 or 365")
    if type(quota) is not int or not 0 <= quota <= 1024**4:
        raise ValueError("Invalid result storage allowance")
    if type(maximum) is not int or not 1 <= maximum <= 1024**3:
        raise ValueError("Invalid single result size")
    return days, quota, maximum


def encode(value):
    return json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), allow_nan=False, sort_keys=True
    ).encode()


class ResultFiles:
    def __init__(self, files, configuration):
        self.files, self.db = files, files.db
        self.root = files.root / "results"
        self.days, self.quota, self.maximum = settings(configuration)
        self.lock = asyncio.Lock()
        self.protection_checks = {}
        self.pending = set()
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS received_results(
                id TEXT PRIMARY KEY, execution_id TEXT NOT NULL, owner TEXT NOT NULL,
                part TEXT NOT NULL, version TEXT NOT NULL, state TEXT NOT NULL,
                bytes INTEGER NOT NULL, created REAL NOT NULL, accessed REAL NOT NULL,
                metadata TEXT NOT NULL, inline BLOB
            );
            CREATE TABLE IF NOT EXISTS result_grants(
                id TEXT NOT NULL, owner TEXT NOT NULL, PRIMARY KEY(id,owner)
            );
            CREATE TABLE IF NOT EXISTS result_holds(
                id TEXT NOT NULL, reason TEXT NOT NULL, PRIMARY KEY(id,reason)
            );
        """)

    def row(self, ident, *, owner=None):
        if not isinstance(ident, str) or not re.fullmatch(r"[a-f0-9]{32}", ident):
            raise ResultAccessError("result_invalid_reference")
        raw = self.db.execute("SELECT * FROM received_results WHERE id=?", (ident,)).fetchone()
        if not raw:
            raise ResultAccessError("result_not_found")
        who = caller() if owner is None else owner
        if (
            who not in {"operator", raw[2]}
            and not self.db.execute(
                "SELECT 1 FROM result_grants WHERE id=? AND owner=?", (ident, who)
            ).fetchone()
        ):
            raise ResultAccessError("result_forbidden")
        row = dict(
            zip(
                (
                    "id",
                    "execution_id",
                    "owner",
                    "part",
                    "version",
                    "state",
                    "bytes",
                    "created",
                    "accessed",
                    "metadata",
                    "inline",
                ),
                raw,
            )
        )
        row["metadata"] = json.loads(row["metadata"])
        return row

    def view(self, row):
        return (
            {
                key: row[key]
                for key in ("id", "execution_id", "part", "version", "state", "bytes", "created", "accessed")
            }
            | row["metadata"]
            | {
                "retention_days": self.days,
                "expires_at": row["accessed"] + self.days * 86400 if self.days else None,
            }
        )

    def grant(self, ident, owner):
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO result_grants VALUES (?,?)", (ident, owner))

    def hold(self, ident, reason, *, commit=True):
        with self.db if commit else nullcontext():
            self.db.execute("INSERT OR IGNORE INTO result_holds VALUES (?,?)", (ident, reason))

    def release(self, reason):
        with self.db:
            self.db.execute("DELETE FROM result_holds WHERE reason=?", (reason,))

    def capture_context(self, references, executions, reason):
        captured = []
        for reference in references:
            record = self.row(reference["id"], owner="operator")
            allowed = record["execution_id"] in executions or any(
                self.db.execute(
                    "SELECT 1 FROM result_grants WHERE id=? AND owner=?", (record["id"], execution)
                ).fetchone()
                for execution in executions
            )
            if not allowed or record["version"] != reference["version"]:
                raise ResultAccessError("result_forbidden")
            self.content(record)
            self.hold(record["id"], reason)
            captured.append(record["id"])
        return sorted(set(captured))

    @staticmethod
    def write(directory, ident, raw):
        # Only host-generated names, pinned directory operations, and immutable bytes.
        target = system.directory(directory)
        try:
            try:
                fd = target.open_file(ident, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
            except FileExistsError:
                with os.fdopen(target.open_file(ident, os.O_RDONLY), "rb") as stream:
                    if stream.read(len(raw) + 1) != raw:
                        raise ResultAccessError("result_content_conflict")
                return
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
                target.sync()
            except BaseException:
                target.unlink(ident)
                raise
        finally:
            target.close()

    def managed_attachments(self, *, ready):
        found = {}
        for (metadata,) in self.db.execute(
            "SELECT metadata FROM received_results WHERE state " + ("=" if ready else "!=") + " 'ready'"
        ):
            for ref in json.loads(metadata).get("attachments", []):
                if ref.get("artifact_name", "").startswith("tool-image-"):
                    found[ref["id"]] = ref
        return found

    def storage_used(self):
        return self.db.execute(
            "SELECT coalesce(sum(bytes),0) FROM received_results WHERE state='ready'"
        ).fetchone()[0] + sum(ref.get("bytes", 0) for ref in self.managed_attachments(ready=True).values())

    def hold_attachments(self, references, reason):
        ids = {ref["id"] for ref in references}
        for ident, metadata in self.db.execute(
            "SELECT id,metadata FROM received_results WHERE state='ready'"
        ).fetchall():
            if ids.intersection(ref["id"] for ref in json.loads(metadata).get("attachments", [])):
                self.hold(ident, reason)

    def touch_attachment(self, ident):
        # Image serving and model attachment reads validate the original received bytes too.
        refs = [
            json.loads(row[0])
            for row in self.db.execute(
                "SELECT a.value FROM received_results r,json_each(r.metadata,'$.attachments') a "
                "WHERE r.state='ready' AND json_extract(a.value,'$.id')=?",
                (ident,),
            )
        ]
        if refs:
            directory = system.directory(self.files.media.folder)
            try:
                try:
                    with os.fdopen(directory.open_file(ident, os.O_RDONLY), "rb") as stream:
                        raw = stream.read(refs[0]["bytes"] + 1)
                except FileNotFoundError:
                    raise ResultAccessError("result_missing") from None
                if any(ref.get("sha256") != hashlib.sha256(raw).hexdigest() for ref in refs):
                    raise ResultAccessError("result_corrupt")
            finally:
                directory.close()
        with self.db:
            self.db.execute(
                "UPDATE received_results SET accessed=max(accessed,?) WHERE state='ready' AND id IN "
                "(SELECT r.id FROM received_results r,json_each(r.metadata,'$.attachments') a WHERE json_extract(a.value,'$.id')=?)",
                (time.time(), ident),
            )

    def purge_expired(self):
        from pathlib import Path

        retained = self.managed_attachments(ready=True)
        expired = self.managed_attachments(ready=False)
        for ident, ref in expired.items():
            if ident in retained:
                continue
            # Only files registered by our received-image adapter; never user uploads or arbitrary sources.
            if not re.fullmatch(r"[a-f0-9]{32}", ident):
                continue
            name = ref.get("artifact_name", "")
            path = Path(ref.get("path", ""))
            if (
                name not in {"tool-image-" + ident + ext for ext in (".png", ".jpg", ".webp")}
                or path.name != name
            ):
                continue
            try:
                path.relative_to(self.files.root)
            except ValueError:
                continue
            for folder, filename in ((path.parent, name), (self.files.media.folder, ident)):
                if not folder.exists():
                    continue
                directory = system.directory(folder)
                try:
                    try:
                        with os.fdopen(directory.open_file(filename, os.O_RDONLY), "rb") as stream:
                            raw = stream.read(ref["bytes"] + 1)
                        # A user-modified independent artifact must not be deleted as our snapshot.
                        if hashlib.sha256(raw).hexdigest() == ref.get("sha256"):
                            directory.unlink(filename)
                    except FileNotFoundError:
                        pass
                finally:
                    directory.close()
        if self.root.exists():
            directory = system.directory(self.root)
            try:
                indexed = {row[0] for row in self.db.execute("SELECT id FROM received_results")}
                # Recover only our host-named interrupted writes, never scan legacy/workspace files.
                for path in self.root.iterdir():
                    if re.fullmatch(r"[a-f0-9]{32}", path.name) and path.name not in indexed | self.pending:
                        directory.unlink(path.name)
                for (ident,) in self.db.execute("SELECT id FROM received_results WHERE state='expired'"):
                    try:
                        directory.unlink(ident)
                    except FileNotFoundError:
                        pass
            finally:
                directory.close()

    async def receive_tool(self, execution_id, call_id, owner, area, payload, images):
        """Trusted execution adapter: all storage admission and image registration stay here."""
        media = None
        try:
            async with self.lock:
                if (
                    not isinstance(images, list)
                    or len(images) > 4
                    or any(
                        not isinstance(image, dict) or not isinstance(image.get("data"), str)
                        for image in images
                    )
                ):
                    raise ResultAccessError("result_invalid_images")
                raw = encode(payload)
                image_size = sum(len(image["data"]) * 3 // 4 for image in images)
                ident = hashlib.sha256(encode([execution_id, "tool:" + call_id])).hexdigest()[:32]
                existing = self.db.execute("SELECT 1 FROM received_results WHERE id=?", (ident,)).fetchone()
                if existing:
                    old = self.row(ident, owner="operator")
                    if old["version"] != hashlib.sha256(raw).hexdigest():
                        raise ResultAccessError("result_content_conflict")
                    self.content(old)  # Never resurrect expired content or claim a missing file is ready.
                else:
                    if len(raw) + image_size > self.maximum:
                        raise ResultAccessError("result_too_large")
                    if self.storage_used() + len(raw) + image_size > self.quota:
                        self._collect()
                        if self.storage_used() + len(raw) + image_size > self.quota:
                            raise ResultAccessError("result_storage_full")
                if images:
                    media = await self.files.tool_media.register(
                        execution_id, call_id, images, area, owner=owner
                    )
                reference = await self._put(
                    execution_id,
                    "tool:" + call_id,
                    owner,
                    payload,
                    attachments=(media or {}).get("attachments", []),
                    complete=media is None or media["state"] == "ready",
                    upstream_complete=payload.get("upstream_complete"),
                )
                receipt = {"state": "ready", "reference": reference}
        except (ValueError, OSError, sqlite3.Error) as error:
            receipt = {
                "state": "failed",
                "issues": [
                    {
                        "code": error.code
                        if isinstance(error, ResultAccessError)
                        else "result_storage_unavailable"
                    }
                ],
            }
        if images and media is None:
            media = {"state": "failed", "attachments": [], "issues": receipt.get("issues", [])}
        receipt["attachments"] = (media or {}).get("attachments", [])
        if media:
            receipt["media"] = media
            if media["state"] != "ready" and receipt["state"] == "ready":
                receipt["state"] = "partial"
        return receipt

    async def put(
        self,
        execution_id,
        part,
        owner,
        payload,
        *,
        attachments=(),
        complete=True,
        upstream_complete=None,
        hold=True,
    ):
        async with self.lock:
            return await self._put(
                execution_id,
                part,
                owner,
                payload,
                attachments=attachments,
                complete=complete,
                upstream_complete=upstream_complete,
                hold=hold,
            )

    async def _put(
        self,
        execution_id,
        part,
        owner,
        payload,
        *,
        attachments=(),
        complete=True,
        upstream_complete=None,
        hold=True,
    ):
        raw = encode(payload)
        ident = hashlib.sha256(encode([execution_id, part])).hexdigest()[:32]
        digest = hashlib.sha256(raw).hexdigest()
        metadata = {
            "complete_received": complete,
            "upstream_complete": upstream_complete,
            "attachments": list(attachments),
        }
        previous = self.db.execute("SELECT version FROM received_results WHERE id=?", (ident,)).fetchone()
        if previous:
            if previous[0] != digest:
                raise ResultAccessError("result_content_conflict")
            row = self.row(ident, owner="operator")
            if row["state"] == "ready":
                self.content(row)
            return self.view(row)
        if len(raw) > self.maximum:
            raise ResultAccessError("result_too_large")
        used = self.storage_used()
        extra = sum(
            ref.get("bytes", 0)
            for ref in attachments
            if ref.get("artifact_name", "").startswith("tool-image-")
            and ref["id"] not in self.managed_attachments(ready=True)
        )
        if used + len(raw) + extra > self.quota:
            self._collect()
            used = self.storage_used()
            if used + len(raw) + extra > self.quota:
                raise ResultAccessError("result_storage_full")
        inline = raw if len(raw) <= 6000 else None
        if inline is None:
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.pending.add(ident)
            pending = asyncio.create_task(asyncio.to_thread(self.write, self.root, ident, raw))
            try:
                await asyncio.shield(pending)
            except asyncio.CancelledError:
                await asyncio.gather(pending, return_exceptions=True)
                raise
            finally:
                self.pending.discard(ident)
        now = time.time()
        with self.db:
            self.db.execute(
                "INSERT INTO received_results VALUES (?,?,?,?,?,'ready',?,?,?,?,?)",
                (ident, execution_id, owner, part, digest, len(raw), now, now, json.dumps(metadata), inline),
            )
            self.db.execute("INSERT OR IGNORE INTO result_grants VALUES (?,?)", (ident, execution_id))
            if hold:
                self.db.execute("INSERT INTO result_holds VALUES (?,?)", (ident, "execution:" + execution_id))
        return self.view(self.row(ident, owner="operator"))

    def content(self, row):
        if row["state"] != "ready":
            raise ResultAccessError("result_" + row["state"])
        try:
            raw = row["inline"]
            if raw is None:
                directory = system.directory(self.root)
                try:
                    with os.fdopen(directory.open_file(row["id"], os.O_RDONLY), "rb") as stream:
                        raw = stream.read(row["bytes"] + 1)
                finally:
                    directory.close()
        except FileNotFoundError:
            raise ResultAccessError("result_missing") from None
        if len(raw) != row["bytes"] or hashlib.sha256(raw).hexdigest() != row["version"]:
            raise ResultAccessError("result_corrupt")
        return raw

    async def request(self, request, *, owner=None, model_limit=None):
        args, action = request.arguments, request.action
        async with self.lock:
            if action in {"result_usage", "result_collect"}:
                if (caller() if owner is None else owner) != "operator":
                    raise ResultAccessError("result_forbidden")
                if set(args) - {"apply", "ids", "version"} or type(args.get("apply", False)) is not bool:
                    raise ResultAccessError("result_invalid_arguments")
                return self._collect(args, apply=action == "result_collect" and args.get("apply") is True)
            allowed = (
                {"id"} if action == "result_get" else {"id", "version", "format", "path", "offset", "length"}
            )
            if action not in {"result_get", "result_read"} or set(args) - allowed:
                raise ResultAccessError("result_invalid_arguments")
            row = self.row(args.get("id"), owner=owner)
            if action == "result_get":
                return self.view(row)
            if args.get("version", row["version"]) != row["version"]:
                raise ResultAccessError("result_version_conflict")
            raw = self.content(row)
            form = args.get("format", "text")
            offset, length = args.get("offset", 0), args.get("length", 6000)
            if type(offset) is not int or offset < 0 or type(length) is not int or not 1 <= length <= CHUNK:
                raise ResultAccessError("result_invalid_range")
            if model_limit is not None:
                length = min(length, model_limit)
            if form == "bytes":
                value = raw
                fragment = base64.b64encode(value[offset : offset + length]).decode()
            elif form in {"text", "json"}:
                value = json.loads(raw)
                path = args.get("path", "/text" if form == "text" else "")
                if not isinstance(path, str) or (path and not path.startswith("/")) or len(path) > 2000:
                    raise ResultAccessError("result_invalid_path")
                try:
                    for key in path.split("/")[1:]:
                        key = key.replace("~1", "/").replace("~0", "~")
                        value = value[int(key)] if isinstance(value, list) and key.isdecimal() else value[key]
                except (KeyError, IndexError, TypeError):
                    raise ResultAccessError("result_path_missing") from None
                if form == "text" or not isinstance(value, (list, dict)):
                    value = value if isinstance(value, str) else encode(value).decode()
                    fragment = value[offset : offset + length].encode()[:CHUNK].decode(errors="ignore")
                else:
                    value = list(value.items()) if isinstance(value, dict) else value
                    fragment = []
                    for item in value[offset : offset + min(length, 100)]:
                        if len(encode([*fragment, item])) > (model_limit or CHUNK):
                            if not fragment:
                                raise ResultAccessError("result_item_requires_text_read")
                            break
                        fragment.append(item)
            else:
                raise ResultAccessError("result_invalid_format")
            if offset > len(value):
                raise ResultAccessError("result_invalid_range")
            count = min(length, len(value) - offset) if form == "bytes" else len(fragment)
            now = time.time()
            with self.db:
                self.db.execute(
                    "UPDATE received_results SET accessed=max(accessed,?) WHERE id=?", (now, row["id"])
                )
            return {
                "id": row["id"],
                "version": row["version"],
                "format": form,
                "data": fragment,
                "offset": offset,
                "next_offset": offset + count,
                "eof": offset + count >= len(value),
                "total": len(value),
            }

    def _collect(self, args=None, *, apply=True):
        args = args or {}
        # Owners reconcile their own lifecycle; unavailable owners conservatively retain holds.
        for (reason,) in self.db.execute("SELECT DISTINCT reason FROM result_holds").fetchall():
            namespace, _, ident = reason.partition(":")
            check = self.protection_checks.get(namespace)
            if check is not None and check(ident) is False:
                self.release(reason)
        rows = self.db.execute(
            "SELECT id,bytes,accessed,EXISTS(SELECT 1 FROM result_holds h WHERE h.id=r.id) "
            "FROM received_results r WHERE state='ready' ORDER BY id"
        ).fetchall()
        version = hashlib.sha256(encode([rows, self.days, self.quota])).hexdigest()
        if args.get("apply") and args.get("version") != version:
            raise ResultAccessError("result_cleanup_conflict")
        ids = args.get("ids")
        if ids is not None and (
            not isinstance(ids, list) or len(ids) > 1000 or any(not isinstance(i, str) for i in ids)
        ):
            raise ResultAccessError("result_invalid_arguments")
        eligible = [
            r
            for r in rows
            if not r[3]
            and (r[0] in ids if ids is not None else self.days and r[2] + self.days * 86400 <= time.time())
        ]
        if ids is not None and set(ids) != {r[0] for r in eligible}:
            raise ResultAccessError("result_cleanup_protected_or_missing")
        removing = {r[0] for r in eligible}
        image_sizes, retained_images = {}, set()
        for ident, metadata in self.db.execute(
            "SELECT id,metadata FROM received_results WHERE state='ready'"
        ):
            for ref in json.loads(metadata).get("attachments", []):
                if ref.get("artifact_name", "").startswith("tool-image-"):
                    if ident in removing:
                        image_sizes[ref["id"]] = ref.get("bytes", 0)
                    else:
                        retained_images.add(ref["id"])
        eligible_bytes = sum(r[1] for r in eligible) + sum(
            size for ident, size in image_sizes.items() if ident not in retained_images
        )
        if apply and eligible:
            directory = system.directory(self.root) if self.root.exists() else None
            try:
                for ident, *_ in eligible:
                    # Tombstone first: interrupted deletion can never make a partial result readable.
                    with self.db:
                        self.db.execute(
                            "UPDATE received_results SET state='expired',inline=NULL WHERE id=?", (ident,)
                        )
                    if directory:
                        try:
                            directory.unlink(ident)
                        except FileNotFoundError:
                            pass
            finally:
                if directory:
                    directory.close()
        if apply:
            self.purge_expired()
        return {
            "version": version,
            "retention_days": self.days,
            "quota_bytes": self.quota,
            "used_bytes": self.storage_used(),
            "protected_count": sum(bool(r[3]) for r in rows),
            "eligible_count": len(eligible),
            "eligible_bytes": eligible_bytes,
            "deleted": len(eligible) if apply else 0,
            "results": [{"id": r[0], "bytes": r[1], "protected": bool(r[3])} for r in rows[:1000]],
        }

    async def collect(self):
        async with self.lock:
            return self._collect()
