"""Bounded, metadata-only device operation lifecycle, separate from voice/tasks."""

import asyncio
import json
from collections import OrderedDict
import time

from .resources import DeviceResources


class DeviceOperations:
    def __init__(self, directory, resources=None, history_limit=128):
        self.directory = directory
        self.resources = resources or DeviceResources()
        if type(history_limit) is not int or not 1 <= history_limit <= 1000:
            raise ValueError("Device operation history limit must be 1..1000")
        self.history_limit = history_limit
        self.routes = {}
        self.records = OrderedDict()
        self.running = {}
        self.db = None
        self.context = None
        self.closed = False
        directory.operations = self

    def register(self, owner, device_id, capability, handler, *, resource=None, shared=False, mutating=False):
        entry = self.directory.entries[device_id]
        if entry["adapter"] != owner or capability not in entry["capabilities"]:
            raise ValueError("Device operation owner/capability mismatch")
        key = (device_id, capability)
        if key in self.routes:
            raise ValueError("Duplicate device operation")
        self.routes[key] = (handler, resource, shared, mutating)

    def capabilities(self, device_id):
        return sorted(cap for ident, cap in self.routes if ident == device_id)

    def bind(self, context):
        self.context = context
        self.db = context.runtime.store.db
        with self.db:
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS device_operations (id TEXT PRIMARY KEY, device_id TEXT NOT NULL, started REAL NOT NULL, state TEXT NOT NULL, metadata TEXT NOT NULL)"
            )
            self.db.execute(
                "CREATE INDEX IF NOT EXISTS device_operations_device ON device_operations(device_id,started DESC)"
            )
            for (payload,) in self.db.execute(
                "SELECT metadata FROM device_operations WHERE state IN ('running','canceling')"
            ).fetchall():
                record = json.loads(payload)
                record.update(state="unknown", error="Process restarted; prior device outcome unconfirmed")
                self._persist(record)
        context.own(self.close)

    def _persist(self, record):
        with self.db:
            self.db.execute(
                "INSERT INTO device_operations VALUES (?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET state=excluded.state,metadata=excluded.metadata",
                (
                    record["operation_id"],
                    record["device_id"],
                    record["started_at"],
                    record["state"],
                    json.dumps(record),
                ),
            )

    def snapshot(self, device_id, operation_id=None):
        if device_id not in self.directory.entries:
            raise ValueError("Unknown device ID")
        if operation_id:
            record = self.records.get(operation_id)
            if record is None and self.db is not None:
                row = self.db.execute(
                    "SELECT metadata FROM device_operations WHERE id=?", (operation_id,)
                ).fetchone()
                record = json.loads(row[0]) if row else None
            if not record or record["device_id"] != device_id:
                raise ValueError("Unknown device operation or device mismatch")
            return dict(record)
        limit = min(self.history_limit, 50)
        rows = (
            self.db.execute(
                "SELECT metadata FROM device_operations WHERE device_id=? ORDER BY started DESC LIMIT ?",
                (device_id, limit),
            ).fetchall()
            if self.db is not None
            else []
        )
        recent = {r["operation_id"]: r for (payload,) in rows if (r := json.loads(payload))}
        for ident in self.running:
            if self.records[ident]["device_id"] == device_id:
                recent[ident] = self.records[ident]
        return {
            "device_id": device_id,
            "operations": sorted(
                (dict(r) for r in recent.values()),
                key=lambda r: (r["state"] in {"running", "canceling"}, r["started_at"]),
                reverse=True,
            )[:50],
            "resources": self.resources.snapshot(device_id),
        }

    def _trim(self):
        ended = [ident for ident in self.records if ident not in self.running]
        for ident in ended[: max(0, len(ended) - self.history_limit)]:
            self.records.pop(ident, None)

    async def execute(self, capability, request):
        if self.closed or self.context is None:
            raise RuntimeError("Device operation owner is not running")
        key = (request.device_id, capability)
        if key not in self.routes:
            raise ValueError(f"Device tool operation not supported: {request.device_id}/{capability}")
        ident = request.operation_id
        if (
            ident in self.records
            or self.db.execute("SELECT 1 FROM device_operations WHERE id=?", (ident,)).fetchone()
        ):
            raise ValueError(
                f"Device operation ID already used: {ident}; query its state instead of replaying"
            )
        # Bound simultaneous operations as well as retained metadata.
        if len(self.running) >= 32:
            raise RuntimeError("Too many active device operations")
        handler, resource, shared, mutating = self.routes[key]
        resource_key = resource(request) if callable(resource) else resource
        lease = (
            self.resources.acquire(request.device_id, resource_key, ident, shared=shared)
            if resource_key
            else None
        )
        record = {
            "operation_id": ident,
            "device_id": request.device_id,
            "capability": capability,
            "state": "running",
            "started_at": time.time(),
            "finished_at": None,
            "duration_seconds": None,
            "error": None,
            "effects_may_have_occurred": mutating,
        }
        try:
            self._persist(record)
        except BaseException:
            if lease:
                lease.release()
            raise
        self.records[ident] = record
        self._trim()
        started = time.monotonic()
        outcome = {}

        async def work():
            try:
                outcome["result"] = await handler(request)
                record["state"] = "completed" if record["state"] != "canceling" else "unknown"
            except asyncio.CancelledError:
                record["state"] = "canceled"
                raise
            except Exception as exc:
                record["state"] = "unknown" if mutating and not isinstance(exc, ValueError) else "failed"
                record["error"] = type(exc).__name__
                outcome["error"] = exc
            finally:
                # The handler must finish its adapter cleanup before its lease is released.
                if lease:
                    lease.release()
                record["finished_at"] = time.time()
                record["duration_seconds"] = round(time.monotonic() - started, 3)
                self._persist(record)

        task = self.context.spawn(work(), "device-operation")
        self.running[ident] = task

        def finished(done):
            # Also runs when canceled before work() enters its try/finally.
            if lease:
                lease.release()
            if record["finished_at"] is None:
                record["state"] = "canceled" if done.cancelled() else "unknown"
                record["finished_at"] = time.time()
                record["duration_seconds"] = round(time.monotonic() - started, 3)
                self._persist(record)
            self.running.pop(ident, None)
            self._trim()

        task.add_done_callback(finished)
        try:
            await asyncio.shield(task)
            if "error" in outcome:
                raise outcome["error"]
            if record["state"] != "completed":
                raise RuntimeError("Device operation was stopped; result discarded")
            return outcome["result"]
        except asyncio.CancelledError:
            record["state"] = "canceling" if not task.done() else record["state"]
            if not task.cancelling():
                task.cancel()
            # A second caller cancellation must not interrupt adapter cleanup.
            # The plugin context retains the worker if the caller leaves early.
            await asyncio.shield(asyncio.gather(task, return_exceptions=True))
            raise
        finally:
            if task.done():
                self.running.pop(ident, None)
            self._trim()

    async def cancel(self, device_id, operation_id):
        record = self.snapshot(device_id, operation_id)
        task = self.running.get(operation_id)
        if task and not task.done():
            self.records[operation_id]["state"] = "canceling"
            self._persist(self.records[operation_id])
            if not task.cancelling():
                task.cancel()
            await asyncio.shield(asyncio.gather(task, return_exceptions=True))
        return self.snapshot(device_id, record["operation_id"])

    async def close(self):
        self.closed = True
        tasks = list(self.running.values())
        for task in tasks:
            if not task.cancelling():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.running.clear()
