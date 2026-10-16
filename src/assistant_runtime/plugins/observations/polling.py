"""Opt-in polling helper for source implementations, never a framework scheduler."""

import asyncio
import time
import uuid


class PollingSource:
    def __init__(self, reporter, adapter, tracker, interval):
        self.reporter, self.adapter, self.tracker_type = reporter, adapter, tracker
        self.interval = interval
        self.health = {"state": "starting"}
        self.retry = asyncio.Event()

    async def run(self):
        snapshot, failures = self.tracker_type(), 0
        try:
            await self.adapter.open()
            while True:
                now = time.time()
                try:
                    current = await self.adapter.sample()
                    now = time.time()
                    prepared = snapshot.prepare(current, now)
                    batch = uuid.uuid4().hex
                    records = [
                        self.reporter.event(
                            event_id=f"{batch}:{index}",
                            type=kind,
                            subject=subject,
                            data=data,
                            observed_start=prepared[3],
                            observed_end=now,
                            evidence="baseline" if kind.endswith("baseline") else "observation",
                        )
                        for index, (kind, subject, data) in enumerate(prepared[0])
                    ]
                    await self.reporter.checkpoint(records, at=now)
                    snapshot.commit(prepared, now)
                    self.health = {"state": "healthy", "last_success": now}
                    failures = 0
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    failures += 1
                    self.health = {
                        "state": "degraded",
                        "failures": failures,
                        "error": "observation_or_recording_unavailable",
                        "error_class": type(exc).__name__,
                        "last_failure": now,
                    }
                    try:
                        await self.reporter.gap("collection_failed", at=snapshot.at or now)
                    except Exception:
                        pass
                    snapshot = self.tracker_type()
                try:
                    await asyncio.wait_for(self.retry.wait(), min(900, self.interval * 2 ** min(failures, 5)))
                    self.retry.clear()
                except TimeoutError:
                    pass
        finally:
            await self.adapter.close()

    async def verify(self):
        try:
            await self.adapter.open()
            sample = await self.adapter.sample()
            return {
                "connected": True,
                "semantics_verified": False,
                "observed_items": len(sample) if "state" not in sample else 1,
                "state": sample.get("state"),
            }
        finally:
            await self.adapter.close()
