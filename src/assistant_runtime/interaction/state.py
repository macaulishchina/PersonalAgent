"""Request versions and bounded operation context, independent of voice connections."""

from collections import deque
from dataclasses import dataclass, field
import time
import uuid

from ..kernel.speech import RequestRef


@dataclass
class Request:
    ref: RequestRef
    text: str
    state: str = "understanding"
    task_id: str | None = None
    run_id: str | None = None
    updated: float = field(default_factory=time.monotonic)
    stages: dict = field(default_factory=dict)


class InteractionState:
    def __init__(self, context_seconds=120):
        self.context_seconds = context_seconds
        self.current = None
        self.trace = deque(maxlen=128)
        self.attending = False

    def begin(self, text, correction=False):
        old = self.current
        ref = (
            RequestRef(old.ref.id, old.ref.revision + 1)
            if old and correction
            else RequestRef(str(uuid.uuid4()), 1)
        )
        self.current = Request(ref, text)
        self.mark(ref, "understanding")
        return self.current

    def valid(self, ref):
        return self.current is not None and self.current.ref == ref

    def mark(self, ref, stage):
        if not self.valid(ref):
            return False
        now = time.monotonic()
        self.current.state = stage
        self.current.updated = now
        self.current.stages[stage] = now
        self.trace.append({"request_id": ref.id, "revision": ref.revision, "stage": stage, "at": now})
        return True

    def context(self, request=None):
        request = request or self.current
        if request is None:
            return {}
        active = request.task_id and request.state in {"executing", "clarifying", "unknown"}
        if not active and time.monotonic() - request.updated > self.context_seconds:
            return {}
        return {"request": request.text, "task_id": request.task_id, "state": request.state}
