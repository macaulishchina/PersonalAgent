"""Bounded producer admission. No hardware, scheduling or protocol knowledge."""

import asyncio
from collections import Counter
import time

from ...kernel.event_reporting import Admission
from .records import validate


class Ingress:
    def __init__(self, storage, context, capacity):
        self.storage, self.context = storage, context
        self.queue = asyncio.Queue(capacity)
        self.accepting = True
        self.capacity = capacity
        self.inflight = 0
        self.lock_users = Counter()
        self.pending = Counter()
        self.drained = {}
        self.locks = {}
        self.losses = {}
        self.loss_versions = Counter()
        self.errors = {}

    def lost(self, source, at, reason):
        self.loss_versions[source] += 1
        self.losses[source] = min(self.losses.get(source, at), at)
        self.errors[source] = reason
        self.health()

    def health(self):
        if self.context.state in {"ready", "degraded"}:
            failed = bool(self.errors or self.losses or getattr(self.storage, "write_error", None))
            self.context.state = "degraded" if failed else "ready"
            self.context.error = "Event recording incomplete; inspect status" if failed else None

    def offer(self, source, record):
        if not self.accepting:
            return Admission(False, "unavailable")
        record = validate(record)  # bounded copy: caller mutation cannot alter an admitted record
        try:
            self.queue.put_nowait((source, record))
        except asyncio.QueueFull:
            self.lost(source, time.time(), "queue_full")
            return Admission(False, "full")
        self.pending[source] += 1
        self.drained.setdefault(source, asyncio.Event()).clear()
        return Admission(True, "queued")

    async def drain(self, source):
        if self.pending[source]:
            await self.drained[source].wait()

    async def write(self, source, action, **args):
        if not self.accepting:
            raise RuntimeError("Event recording is stopping")
        if self.inflight >= self.capacity:
            raise RuntimeError("Event write admission full")
        if action == "append":
            args["record"] = validate(args["record"])
        self.inflight += 1
        try:
            return await self.commit(source, action, **args)
        finally:
            self.inflight -= 1

    async def commit(self, source, action, **args):
        lock = self.locks.setdefault(source, asyncio.Lock())
        self.lock_users[source] += 1
        try:
            async with lock:
                if source in self.losses:
                    at = self.losses[source]
                    version = self.loss_versions[source]
                    await self.storage.call("coverage", source=source, at=at, reason="ingress_loss")
                    if self.loss_versions[source] == version:
                        self.losses.pop(source, None)
                try:
                    result = await self.storage.call(action, source=source, **args)
                except Exception:
                    self.errors[source] = "write_failed"
                    self.health()
                    raise
                if action in {"append", "snapshot"}:
                    self.errors.pop(source, None)
                    if result.get("cursor") and not result.get("duplicate"):
                        self.context.publish("events.committed", source_id=source, cursor=result["cursor"])
                self.health()
                return result

        finally:
            self.lock_users[source] -= 1
            if not self.lock_users[source]:
                del self.lock_users[source]
                del self.locks[source]

    async def consume(self, source, record):
        try:
            await self.commit(source, "append", record=record)
        except BaseException:
            self.lost(
                source,
                record.get("occurred_at") or record.get("observed_start") or time.time(),
                "queued_write_failed",
            )
            raise
        finally:
            self.pending[source] -= 1
            if not self.pending[source]:
                self.drained[source].set()
            self.queue.task_done()

    async def worker(self):
        while True:
            source, record = await self.queue.get()
            try:
                await self.consume(source, record)
            except Exception:
                pass  # failure remains visible; never infer persistence from admission

    async def close(self):
        # Context cancels its worker first. Drain accepted bounded work before closing SQLite.
        self.accepting = False
        while not self.queue.empty():
            source, record = self.queue.get_nowait()
            try:
                await self.consume(source, record)
            except Exception:
                pass
        for source, at in list(self.losses.items()):
            try:
                await self.storage.call("coverage", source=source, at=at, reason="ingress_loss")
            except Exception:
                pass

    def status(self):
        return {
            "queued": self.queue.qsize(),
            "inflight": self.inflight,
            "losses": dict(self.losses),
            "errors": dict(self.errors),
        }
