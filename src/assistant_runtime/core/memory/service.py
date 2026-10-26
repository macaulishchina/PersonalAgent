"""Memory policy and durable capture, separate from retrieval and task execution."""

from .declaration import DECLARATION

import asyncio
import time
import uuid

from assistant_runtime.api.contracts import MemoryRequest
from assistant_runtime.api.ai_usage import UsageScope, current_usage


class MemoryPlugin:
    manifest = DECLARATION.manifest

    def __init__(self, config, store, provider, foreground):
        self.config, self.store, self.provider = config, store, provider
        self.records = store.memory
        self.foreground = foreground
        self.lock = asyncio.Lock()
        self.context = None
        self.current = None
        self.maintenance_busy = False
        self.draining = False
        self.ready = False
        self.cache = []
        self.retry_after = 0.0

    async def start(self, context):
        self.context = context
        context.own(self.provider.close)
        context.register(MemoryRequest, self.handle)
        context.spawn(self.run(), "memory-worker")

    async def prepare_configuration(self, config):
        from assistant_runtime.core.configuration.updates import attributes

        # Captures already accepted keep their work identity; policy applies to new captures.
        return attributes([(self, "config", config)])

    def maintenance_pause(self):
        self.draining = True

    def maintenance_resume(self):
        self.draining = False

    def maintenance_pending(self):
        return [{"state": "memory_capture"}] if self.maintenance_busy else []

    def enqueue(self):
        if self.config.memory_auto_capture:
            self.records.enqueue()

    def preempt(self):
        if self.current and not self.current.done():
            self.current.cancel()

    async def handle(self, request):
        a = request.arguments
        if request.action in {"remember", "forget", "import"}:
            self.preempt()
        async with self.lock:
            if request.action == "search":
                records = await self.provider.search(a["query"], a.get("scope", "user"))
                return {"records": [r for r in records if self.visible(r)]}
            if request.action == "read":
                return {"record": None if self.deleted(a["id"]) else await self.provider.read(a["id"])}
            if request.action == "export":
                return {"records": [r for r in await self.provider.export() if self.visible(r)]}
            if request.action == "import":
                for record in a["records"]:
                    # Revisions and tombstones belong to this installation. Never revive a deletion.
                    if not self.deleted(record["id"]):
                        await self._save(record, self.store.get("memory.epoch", 0), imported=True)
                await self.refresh()
                return {"ok": True}
            if request.action == "forget":
                ident = a["id"]
                self.invalidate()
                with self.store.db:
                    self.store.db.execute(
                        "INSERT INTO memory_items VALUES(?,1,1,?,?) ON CONFLICT(id) DO UPDATE SET revision=revision+1,deleted=1,updated=excluded.updated",
                        (ident, self.store.transcripts.last_id(), time.time()),
                    )
                # Tombstone first: even a failed upstream delete must disappear from recall.
                await self.refresh()
                await self.provider.delete(ident)
                return {"ok": True, "id": ident, "history_erased": False}
            if request.action == "remember":
                self.invalidate()
                record = dict(a)
                record.setdefault("id", uuid.uuid4().hex)
                record.setdefault("scope", "user")
                record["source"] = {
                    "kind": "explicit",
                    "session": self.store.get("session_id"),
                    "transcript_id": self.store.transcripts.last_id(),
                    "time": time.time(),
                }
                saved = await self._save(record, self.store.get("memory.epoch", 0))
                await self.refresh()
                return {"ok": True, "record": saved}
            raise ValueError("Unknown memory action")

    def deleted(self, ident):
        row = self.store.db.execute("SELECT deleted FROM memory_items WHERE id=?", (ident,)).fetchone()
        return bool(row and row[0])

    def invalidate(self):
        # Conservatively invalidate every older queued batch. A new fact may be learned
        # from later input, but forgetting cannot resurrect it from an old source range.
        self.store.set("memory.epoch", self.store.get("memory.epoch", 0) + 1)
        self.store.set("memory.cursor", self.store.transcripts.last_id())

    async def _save(self, record, epoch, imported=False):
        if epoch != self.store.get("memory.epoch", 0):
            raise ValueError("Stale memory source")
        ident = record["id"]
        if (
            record.get("scope") not in {"user", "project"}
            or not record.get("title")
            or not record.get("text")
        ):
            raise ValueError("Invalid memory content/scope")
        if len(record["text"]) > 4000 or len(record["title"]) > 200:
            raise ValueError("Memory must be a bounded fact, not a transcript")
        row = self.store.db.execute(
            "SELECT revision,deleted FROM memory_items WHERE id=?", (ident,)
        ).fetchone()
        if row and row[1]:
            raise ValueError("Deleted memory cannot be revived; use a new fact from new input")
        revision = row[0] if row else 0
        existing = await self.provider.read(ident)
        if existing and (
            existing["scope"] != record["scope"]
            or (
                record["scope"] == "project"
                and existing.get("project", "")
                != record.get("project", getattr(self.config, "workspace", ""))
            )
        ):
            raise ValueError("Memory scope is immutable; create a new scoped record")
        if (
            existing
            and existing.get("text") == record["text"]
            and existing.get("source") == record.get("source")
        ):
            return existing  # Retry after write or process crash, without duplicate facts.
        if row and not imported and record.get("revision") != revision:
            raise ValueError("Memory revision conflict; read current record before updating")
        record = {**record, "revision": revision + 1, "updated": time.time()}
        if record["scope"] == "project":
            record.setdefault("project", getattr(self.config, "workspace", ""))
        await self.provider.upsert(record)
        with self.store.db:
            self.store.db.execute(
                "INSERT OR REPLACE INTO memory_items VALUES(?,?,0,?,?)",
                (ident, revision + 1, self.store.transcripts.last_id(), time.time()),
            )
        return record

    def visible(self, record):
        return not self.deleted(record["id"]) and (
            record.get("scope") != "project" or record.get("project") == getattr(self.config, "workspace", "")
        )

    async def refresh(self):
        self.cache = [r for r in await self.provider.export() if self.visible(r)]

    def context_text(self):
        # Read from a ready snapshot; network/index startup never delays wake feedback.
        records = sorted(self.cache, key=lambda r: r.get("updated", 0), reverse=True)[:8]
        return "\n".join(f"[{r['scope']}:{r['id']}] {r['text']}" for r in records)[:3500]

    async def process_job(self, job):
        ident, start, end, epoch = job
        # One durable capture batch owns user/project extraction, retries and restart.
        # Foreground preemption must not replenish its model allowance.
        token = current_usage.set(UsageScope(f"memory:{epoch}:{start}:{end}", f"memory:{ident}"))
        try:
            await self._process_job(job)
        finally:
            current_usage.reset(token)

    async def _process_job(self, job):
        ident, start, end, epoch = job
        status = self.store.db.execute("SELECT status FROM memory_jobs WHERE id=?", (ident,)).fetchone()
        if status and status[0] == "done":
            return
        if epoch != self.store.get("memory.epoch", 0):
            self.store.db.execute("UPDATE memory_jobs SET status='stale' WHERE id=?", (ident,))
            self.store.db.commit()
            return
        # A provider task completion is a model report, not independent evidence.
        sources = [s for s in self.store.transcripts.between(start, end) if s["role"] == "user"]
        if len(sources) > 64 or sum(len(s["text"]) for s in sources) > 32000:
            raise ValueError("Memory source batch exceeds its bounded input limit")
        async with self.lock:
            for scope in ("user", "project"):
                if self.foreground() or epoch != self.store.get("memory.epoch", 0):
                    raise asyncio.CancelledError
                outcome = (
                    await self.provider.capture(
                        sources,
                        scope,
                        getattr(self.config, "workspace", ""),
                        f"{epoch}:{start}:{end}:{scope}",
                    )
                    if sources
                    else {"records": [], "deleted": []}
                )
                if self.foreground() or epoch != self.store.get("memory.epoch", 0):
                    raise asyncio.CancelledError
                with self.store.db:
                    for record in outcome["records"]:
                        if not self.deleted(record["id"]):
                            self.store.db.execute(
                                "INSERT OR REPLACE INTO memory_items VALUES(?,?,0,?,?)",
                                (record["id"], record["revision"], end, record["updated"]),
                            )
                    for deleted in outcome.get("deleted", []):
                        self.store.db.execute(
                            "INSERT INTO memory_items VALUES(?,1,1,?,?) ON CONFLICT(id) DO UPDATE SET deleted=1,revision=revision+1,updated=excluded.updated",
                            (deleted, end, time.time()),
                        )
            await self.refresh()
            self.store.db.execute("UPDATE memory_jobs SET status='done',error=NULL WHERE id=?", (ident,))
            self.store.db.commit()

    async def run(self):
        from assistant_runtime.api.lifecycle import configuration_boundary

        while True:
            await configuration_boundary(self.context)
            if self.draining:
                await asyncio.sleep(0.2)
                continue
            self.maintenance_busy = True
            try:
                if not self.ready:
                    await self.provider.start()
                    await self.refresh()
                    self.ready = True
                    self.context.error = None
                    self.context.state = "ready"
                # Retry failed physical deletes; tombstones already exclude recall.
                for (ident,) in self.store.db.execute(
                    "SELECT id FROM memory_items WHERE deleted=1"
                ).fetchall():
                    await self.provider.delete(ident)
                if (
                    not self.foreground()
                    and self.config.memory_auto_capture
                    and time.monotonic() >= self.retry_after
                ):
                    row = self.store.db.execute(
                        "SELECT id,start_id,end_id,epoch FROM memory_jobs WHERE status='pending' AND attempts<5 ORDER BY id LIMIT 1"
                    ).fetchone()
                    if row:
                        self.store.db.execute(
                            "UPDATE memory_jobs SET status='running',attempts=attempts+1 WHERE id=?",
                            (row[0],),
                        )
                        self.store.db.commit()
                        self.current = asyncio.create_task(self.process_job(row))
                        try:
                            await self.current
                            self.context.error = None
                            self.context.state = "ready"
                        except asyncio.CancelledError:
                            if asyncio.current_task().cancelling():
                                raise
                            self.store.db.execute(
                                "UPDATE memory_jobs SET status='pending',attempts=max(0,attempts-1) WHERE id=?",
                                (row[0],),
                            )
                            self.store.db.commit()
                        except Exception as exc:
                            self.store.db.execute(
                                "UPDATE memory_jobs SET status='pending',error=? WHERE id=?",
                                (str(exc)[:200], row[0]),
                            )
                            self.store.db.commit()
                            self.context.error = str(exc)[:200]
                            self.context.state = "degraded"
                            attempts = self.store.db.execute(
                                "SELECT attempts FROM memory_jobs WHERE id=?", (row[0],)
                            ).fetchone()[0]
                            self.retry_after = time.monotonic() + min(300, 30 * 2 ** max(0, attempts - 1))
                        finally:
                            self.current = None
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.ready = False
                self.context.state = "degraded"
                self.context.error = str(exc)[:200]
            finally:
                self.maintenance_busy = False
            await asyncio.sleep(5)
