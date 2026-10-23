"""Finite work admission shared by core entrances; no execution or restart policy."""

from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
import uuid

from .ai_usage import current_usage
from .plugins import current_call

_accepted = ContextVar("accepted_work", default=None)


class WorkIntake:
    def __init__(self):
        self.paused = False
        self.pending = {}

    @contextmanager
    def accept(self, label):
        # These identities are bound by the host, never taken from request arguments.
        parent = _accepted.get()
        inherited = (
            (parent is not None and parent["active"])
            or current_usage.get() is not None
            or current_call.get() is not None
        )
        if self.paused and not inherited:
            raise RuntimeError("Service is waiting to restart; new work is paused")
        ident = uuid.uuid4().hex
        self.pending[ident] = label
        lease = {"active": True}
        token = _accepted.set(lease)
        try:
            yield
        finally:
            lease["active"] = False
            _accepted.reset(token)
            self.pending.pop(ident, None)


def admits_work(predicate=lambda *a, **kw: True):
    """Track admission through asynchronous preparation, before durable work exists."""

    def decorate(function):
        @wraps(function)
        async def call(self, *args, **kwargs):
            intake = getattr(self, "intake", None) or getattr(getattr(self, "host", None), "intake", None)
            if intake is None or not predicate(*args, **kwargs):
                return await function(self, *args, **kwargs)
            with intake.accept(function.__qualname__):
                return await function(self, *args, **kwargs)

        return call

    return decorate
