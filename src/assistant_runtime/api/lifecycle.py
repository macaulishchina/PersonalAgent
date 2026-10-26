"""Finite work admission shared by core entrances; no execution or restart policy."""

from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
import uuid

from .ai_usage import current_usage
from .plugins import current_call

_accepted = ContextVar("accepted_work", default=None)


async def configuration_boundary(context):
    """Pause new background dispatch while a host configuration transaction switches owners."""
    event = getattr(getattr(context, "runtime", None), "configuration_ready", None)
    if event is not None:
        await event.wait()
    if getattr(context, "state", None) in {"failed", "blocked"}:
        import asyncio

        await context.cancelled.wait()
        raise asyncio.CancelledError()


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


async def wait_thread(function, *args, completed=None, cancel=None):
    """Retain an async owner's lease until its bounded worker actually exits.

    Cancellation can request an OS stop through cancel, but never detaches the
    thread. Completion publication stays on the owning event loop.
    """
    import asyncio

    task = asyncio.create_task(asyncio.to_thread(function, *args))
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
            if cancel is not None:
                cancel()
    result = task.result()
    if completed is not None:
        completed(result)
    if cancelled:
        raise asyncio.CancelledError
    return result
