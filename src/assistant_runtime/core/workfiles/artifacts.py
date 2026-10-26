"""Immutable, execution-owned publications; explicit expiry and resumable reads."""

import asyncio
import base64
import hashlib
import json
import math
import os
import stat
import time
import uuid

from assistant_runtime.platforms.directory import Directory, component
from .inputs import caller

MAX_BYTES = 64 * 1024 * 1024
CHUNK_BYTES = 1024 * 1024


class ArtifactFiles:
    def __init__(self, files):
        self.files, self.db = files, files.db
        self.root = files.root / "published"
        self.lock = asyncio.Lock()
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS published_artifacts(
                id TEXT PRIMARY KEY, owner TEXT NOT NULL, execution_id TEXT NOT NULL,
                intent TEXT NOT NULL, state TEXT NOT NULL, data TEXT NOT NULL,
                expires REAL NOT NULL, created REAL NOT NULL
            );
        """)
        with self.db:
            self.db.execute("UPDATE published_artifacts SET state='unavailable' WHERE state='preparing'")

    async def collect(self):
        async with self.lock:
            if not self.root.exists():
                return
            directory = Directory(self.root)
            try:
                for (ident,) in self.db.execute(
                    "SELECT id FROM published_artifacts WHERE expires<=? AND state!='collected'",
                    (time.time(),),
                ).fetchall():
                    try:
                        directory.unlink(ident)
                    except FileNotFoundError:
                        pass
                    with self.db:
                        self.db.execute(
                            "UPDATE published_artifacts SET state='collected' WHERE id=?", (ident,)
                        )
            finally:
                directory.close()

    def get(self, ident, *, controlled=False):
        try:
            ident = str(uuid.UUID(ident))
        except (ValueError, TypeError, AttributeError):
            raise ValueError("Invalid artifact reference") from None
        raw = self.db.execute("SELECT * FROM published_artifacts WHERE id=?", (ident,)).fetchone()
        if not raw or caller() not in {"operator", raw[1]}:
            raise ValueError("Artifact is unavailable to this caller")
        row = dict(zip(("id", "owner", "execution_id", "intent", "state", "data", "expires", "created"), raw))
        row["data"] = json.loads(row["data"])
        if row["data"].get("control_binding") and not controlled:
            raise PermissionError(
                "Controlled artifacts require the execution control handle and a valid grant"
            )
        return row

    @staticmethod
    def view(row):
        expired = row["expires"] <= time.time()
        return {
            "id": row["id"],
            "execution_id": row["execution_id"],
            "created": row["created"],
            "state": "expired" if expired else row["state"],
            "expires_at": row["expires"],
            "readable": not expired and row["state"] == "published",
            **row["data"],
        }

    @staticmethod
    def expiry(value):
        if type(value) not in (int, float) or not math.isfinite(value) or value <= time.time():
            raise ValueError("Artifact requires a finite future expires_at")
        return value

    async def request(self, request, *, _control=None):
        args = request.arguments
        async with self.lock:
            control = _control() if _control else None
            if request.action == "finalize":
                result = await self.finalize(args, control=control)
                if _control:
                    _control()
                return result
            allowed = {
                "artifact_get": {"id"},
                "artifact_read": {"id", "offset", "length"},
                "artifact_retain": {"id", "expires_at"},
                "artifact_release": {"id"},
            }.get(request.action)
            if allowed is None or set(args) - allowed or "id" not in args:
                raise ValueError("Unknown artifact operation or fields")
            row = self.get(args["id"], controlled=control is not None)
            if request.action == "artifact_get":
                return self.view(row)
            if request.action == "artifact_retain":
                if not self.view(row)["readable"]:
                    raise ValueError("Artifact is expired or not published")
                expiry = self.expiry(args.get("expires_at"))
                with self.db:
                    self.db.execute(
                        "UPDATE published_artifacts SET expires=max(expires,?) WHERE id=?",
                        (expiry, row["id"]),
                    )
                return self.view(self.get(row["id"], controlled=control is not None))
            if request.action == "artifact_release":
                with self.db:
                    self.db.execute(
                        "UPDATE published_artifacts SET expires=? WHERE id=?", (time.time(), row["id"])
                    )
                return self.view(self.get(row["id"], controlled=control is not None))
            if not self.view(row)["readable"]:
                raise ValueError("Artifact is expired or not published")
            offset, length = args.get("offset", 0), args.get("length", CHUNK_BYTES)
            if type(offset) is not int or not 0 <= offset <= row["data"]["bytes"]:
                raise ValueError("Invalid artifact offset")
            if type(length) is not int or not 1 <= length <= CHUNK_BYTES:
                raise ValueError("Artifact read length must be 1..1048576")
            # The lock is a read lease: release/collection cannot remove an active read.
            data = await self.thread(self.read, row, offset, length)
            if _control:
                _control()
            return {
                **self.view(row),
                "offset": offset,
                "data": base64.b64encode(data).decode(),
                "next_offset": offset + len(data),
                "eof": offset + len(data) == row["data"]["bytes"],
            }

    async def finalize(self, args, *, control=None):
        if set(args) != {"finalize_id", "execution_id", "name", "expires_at"}:
            raise ValueError("Finalize requires finalize_id, execution_id, name and expires_at")
        try:
            ident = str(uuid.UUID(args["finalize_id"]))
        except (ValueError, TypeError, AttributeError):
            raise ValueError("Finalize requires a stable UUID") from None
        name = component(args["name"])
        intent = json.dumps(args, sort_keys=True)
        existing = self.db.execute("SELECT id FROM published_artifacts WHERE id=?", (ident,)).fetchone()
        if existing:
            row = self.get(ident, controlled=control is not None)
            if row["intent"] != intent:
                raise ValueError("Finalize ID reused with different content")
            # An interrupted publication is inspectable, never silently re-snapshotted.
            return self.view(row)
        expiry = self.expiry(args["expires_at"])
        if not self.db.execute("SELECT 1 FROM sqlite_master WHERE name='agent_runs'").fetchone():
            raise ValueError("Execution capability is required to publish an artifact")
        run = self.db.execute(
            "SELECT owner,state,data FROM agent_runs WHERE id=?", (args["execution_id"],)
        ).fetchone()
        if not run or caller() not in {"operator", run[0]}:
            raise ValueError("Execution is unavailable to this caller")
        if run[1] not in {"completed", "failed", "cancelled", "interrupted"}:
            raise ValueError("Execution must have a known terminal outcome before publication")
        data = json.loads(run[2])
        if data.get("context_id") and control is None:
            raise PermissionError("Controlled publication requires a verified resource grant")
        owner = data.get("work_area") or "executions/" + args["execution_id"]
        source = control["source"] if control else self.files.area(owner) / "artifacts"
        metadata = {k: v for k, v in (control or {}).items() if k != "source"}
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        reserved = sum(
            json.loads(raw).get("bytes", MAX_BYTES)
            for (raw,) in self.db.execute(
                "SELECT data FROM published_artifacts WHERE state IN ('preparing','published','unavailable')"
            )
        )
        if reserved + MAX_BYTES > self.files.artifact_storage_bytes:
            raise ValueError("Artifact storage quota exhausted; release and collect expired publications")
        with self.db:
            self.db.execute(
                "INSERT INTO published_artifacts VALUES(?,?,?,?,?,?,?,?)",
                (
                    ident,
                    run[0],
                    args["execution_id"],
                    intent,
                    "preparing",
                    json.dumps({"name": name, **metadata}),
                    expiry,
                    time.time(),
                ),
            )
        # Complete the bounded snapshot even if the caller disconnects; do not release
        # the publication lock while an off-thread writer still owns the destination.
        try:
            await self.thread(
                self.snapshot, source, name, ident, completed=lambda result: self.publish(ident, name, result)
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            with self.db:
                self.db.execute("UPDATE published_artifacts SET state='unavailable' WHERE id=?", (ident,))
            raise
        return self.view(self.get(ident, controlled=control is not None))

    @staticmethod
    async def thread(function, *args, completed=None):
        from assistant_runtime.api.lifecycle import wait_thread

        return await wait_thread(function, *args, completed=completed)

    def publish(self, ident, name, metadata):
        previous = json.loads(
            self.db.execute("SELECT data FROM published_artifacts WHERE id=?", (ident,)).fetchone()[0]
        )
        with self.db:
            self.db.execute(
                "UPDATE published_artifacts SET state='published',data=? WHERE id=? AND state='preparing'",
                (json.dumps({**previous, "name": name, **metadata}), ident),
            )

    def snapshot(self, source, name, ident):
        src, dst = Directory(source), None
        try:
            dst = Directory(self.root)
            with os.fdopen(src.open_file(name, os.O_RDONLY, frozen=True), "rb") as incoming:
                info = os.fstat(incoming.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_BYTES:
                    raise ValueError("Artifact must be one regular file of at most 64 MiB without hard links")
                if dst.free_bytes() < info.st_size:
                    raise ValueError("Insufficient artifact storage space")
                digest, count = hashlib.sha256(), 0
                with os.fdopen(dst.open_file(ident, os.O_WRONLY | os.O_CREAT | os.O_EXCL), "wb") as output:
                    while chunk := incoming.read(CHUNK_BYTES):
                        count += len(chunk)
                        if count > MAX_BYTES:
                            raise ValueError("Artifact exceeds 64 MiB")
                        digest.update(chunk)
                        output.write(chunk)
                    output.flush()
                    os.fsync(output.fileno())
                dst.sync()
                if count != info.st_size:
                    raise ValueError("Artifact source changed during publication")
                return {"bytes": count, "sha256": digest.hexdigest()}
        finally:
            src.close()
            if dst:
                dst.close()

    def read(self, row, offset, length):
        directory = Directory(self.root)
        try:
            with os.fdopen(directory.open_file(row["id"], os.O_RDONLY, frozen=True), "rb") as stream:
                # Check before returning any bytes, including resumed requests.
                digest, count = hashlib.sha256(), 0
                while chunk := stream.read(CHUNK_BYTES):
                    count += len(chunk)
                    if count > MAX_BYTES:
                        raise ValueError("Published artifact integrity check failed")
                    digest.update(chunk)
                if count != row["data"]["bytes"] or digest.hexdigest() != row["data"]["sha256"]:
                    raise ValueError("Published artifact integrity check failed")
                stream.seek(offset)
                return stream.read(length)
        finally:
            directory.close()
