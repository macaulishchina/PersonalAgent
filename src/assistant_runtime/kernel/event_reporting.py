"""Source-bound durable reporting. Standard library only; independent of storage."""

import asyncio
from dataclasses import dataclass
import re
import time
from typing import Any, Protocol
import uuid


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", value):
        raise ValueError("Invalid event/source identifier")
    return value


@dataclass(frozen=True)
class Admission:
    accepted: bool
    reason: str  # queued, unavailable, invalid, full, closed


class EventSink(Protocol):
    async def write(self, source: str, action: str, **args: Any) -> dict: ...
    def offer(self, source: str, record: dict) -> Admission: ...
    async def drain(self, source: str) -> None: ...


class EventReporter:
    """Trusted plugin handle. Retrying requires the same event_id AND event content.

    report confirms commit; offer confirms admission only. Calls are event-loop local.
    Advanced sources use checkpoint/gap to declare continuous observation coverage.
    """

    def __init__(self, context, source):
        self.context, self.source = context, identifier(source)
        self.generation = context.generation
        self.closed = False

    def sink(self):
        if self.closed or self.context.generation != self.generation:
            return None
        return self.context.runtime.event_sink

    def event(
        self,
        *,
        type,
        subject,
        data,
        event_id=None,
        occurred_at=None,
        observed_start=None,
        observed_end=None,
        evidence="observation",
        version=1,
    ):
        record = {
            "key": event_id or uuid.uuid4().hex,
            "type": type,
            "subject": subject,
            "data": data,
            "evidence": evidence,
            "version": version,
        }
        if observed_start is not None or observed_end is not None:
            if occurred_at is not None:
                raise ValueError("Choose occurrence time or observation interval")
            record.update(observed_start=observed_start, observed_end=observed_end)
        else:
            # This is a local observation timestamp, not an inferred device clock.
            record["occurred_at"] = time.time() if occurred_at is None else occurred_at
        return record

    async def report(self, **event):
        return await self.write("append", record=self.event(**event))

    def offer(self, **event):
        sink = self.sink()
        if sink is None:
            return Admission(False, "closed" if self.closed else "unavailable")
        try:
            return sink.offer(self.source, self.event(**event))
        except (ValueError, TypeError):
            return Admission(False, "invalid")

    async def write(self, action, **args):
        sink = self.sink()
        if sink is None:
            raise RuntimeError("Event recording unavailable")
        return await asyncio.wait_for(sink.write(self.source, action, **args), 15)

    async def checkpoint(self, records=(), *, at=None):
        """Atomically commit a complete observation and advance coverage."""
        return await self.write("snapshot", records=list(records), at=time.time() if at is None else at)

    async def gap(self, reason, *, at=None):
        return await self.write("coverage", reason=reason, at=time.time() if at is None else at)

    async def close(self):
        sink = self.context.runtime.event_sink
        self.closed = True
        if sink is not None:
            await sink.drain(self.source)
