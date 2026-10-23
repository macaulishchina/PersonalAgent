"""Single local output owner, durable at-most-once receipts and cancellation fences."""

import asyncio
import time


class OutputScheduler:
    def __init__(self, player, renderer, database, allowed):
        self.player, self.renderer, self.db, self.allowed = player, renderer, database, allowed
        self.lock = asyncio.Lock()
        self.generation = 0
        self.active = False
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS speech_receipts("
            "request_id TEXT,revision INTEGER,kind TEXT,state TEXT,updated REAL,"
            "PRIMARY KEY(request_id,revision,kind))"
        )
        self.db.execute(
            "UPDATE speech_receipts SET state='unknown' WHERE state IN ('submitted','outputting')"
        )
        self.db.commit()

    def receipt(self, intent, status):
        with self.db:
            self.db.execute(
                "UPDATE speech_receipts SET state=?,updated=? WHERE request_id=? AND revision=? AND kind=?",
                (status, time.time(), intent.request.id, intent.request.revision, intent.kind),
            )

    async def interrupt(self):
        self.generation += 1
        await self.player.stop(suppress=False)

    async def say(self, intent, valid=lambda: True, on_started=None):
        generation = self.generation

        def eligible():
            return (
                generation == self.generation
                and self.allowed()
                and valid()
                and time.monotonic() < intent.expires_at
            )

        if intent.kind == "completed" and intent.perceptible and not intent.important:
            return "omitted"
        if not eligible():
            return "deferred"
        async with self.lock:
            if not eligible():
                return "deferred"
            with self.db:
                inserted = self.db.execute(
                    "INSERT OR IGNORE INTO speech_receipts VALUES(?,?,?,'submitted',?)",
                    (intent.request.id, intent.request.revision, intent.kind, time.time()),
                ).rowcount
            if not inserted:
                return "duplicate"
            self.active = True
            status = "unknown"
            epoch = None
            try:
                pcm = await asyncio.wait_for(self.renderer.render(intent.text), 15)
                if not eligible():
                    status = "interrupted"
                    return status
                self.receipt(intent, "outputting")
                self.player.manual_muted = False
                await self.player.stop(suppress=False)
                epoch = self.player.epoch
                discarded = self.player.discarded_bytes
                previous_write = getattr(self.player, "last_voice_write", 0)
                started = False
                # Pace against the shared output clock; never fill a 1.5 s queue
                # with an entire synthesized sentence in one synchronous call.
                for offset in range(0, len(pcm), self.player.frame_bytes):
                    if not eligible() or self.player.epoch != epoch:
                        status = "interrupted"
                        if self.player.epoch == epoch:
                            await self.player.stop(suppress=False)
                        return status
                    part = pcm[offset : offset + self.player.frame_bytes]
                    self.player.feed(part.ljust(self.player.frame_bytes, b"\0"))
                    await asyncio.sleep(0.02)
                    written = getattr(self.player, "last_voice_write", 0)
                    if not started and written > previous_write:
                        started = True
                        if on_started:
                            on_started(written)
                await asyncio.wait_for(self.player.queue.join(), 3)
                status = (
                    "completed"
                    if eligible()
                    and self.player.epoch == epoch
                    and self.player.discarded_bytes == discarded
                    and not self.player.last_error
                    else "unknown"
                )
                return status
            except asyncio.CancelledError:
                status = "interrupted"
                if epoch is not None and self.player.epoch == epoch:
                    await self.player.stop(suppress=False)
                raise
            except Exception:
                if epoch is not None and self.player.epoch == epoch:
                    await self.player.stop(suppress=False)
                return status
            finally:
                self.active = False
                self.receipt(intent, status)

    async def close(self):
        await self.interrupt()
        await self.renderer.close()
