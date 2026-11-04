"""One bounded admission and durable attempt identity for every execution entrance."""

import asyncio
import uuid
from assistant_runtime.api.capacity import Capacity

from .records import TERMINAL


class Admission:
    def __init__(self, service):
        self.service = service
        self.slots = Capacity(service.concurrency)
        self.waiting = {}
        self.leases = set()
        self.external = set()
        self.dispatching = set()

    def process_observed(self, session, evidence):
        ident = self.service.sessions.get(session)
        if ident is not None:
            with self.service.records.db:
                self.service.records.operations.observe(ident, None, {"kind": "local_process", **evidence})

    def operation(self, session, kind, native_id, state=None, **details):
        if kind == "model" and state is None:
            self.service.controlled.authorize_session(session)
        ident = self.service.sessions.get(session)
        if ident is None:
            return None
        operations = self.service.records.operations
        if state is None:
            return operations.begin(ident, kind, native_id, **details)
        operations.settle(ident, kind, native_id, state, **details)

    def inputs_consumed(self, session, turn, model_request, identities):
        ident = self.service.sessions.get(session)
        if ident is None:
            return
        row = self.service.records.get(ident)
        if row["state"] not in TERMINAL and row["data"].get("turn_id") == turn:
            self.service.records.messages.consumed(ident, turn, model_request, identities)

    def release(self, ident):
        if ident in self.leases:
            self.leases.remove(ident)
            self.slots.release()

    def cancel_waiting(self, ident):
        signal = self.waiting.get(ident)
        if signal is not None:
            signal.set()
            return True
        return False

    async def start(self, arguments, dispatch):
        service = self.service
        if service.stopping:
            raise RuntimeError("Execution service is stopping")
        session = arguments["session_id"]
        prior = service.sessions.get(session)
        if prior in self.waiting or prior in self.leases or prior in self.dispatching:
            raise RuntimeError("Execution session already has an active attempt")
        if prior and prior in self.external and service.records.get(prior)["state"] not in TERMINAL:
            raise RuntimeError("Execution session already has an active attempt")
        if len(self.waiting) >= service.queue.maxsize:
            raise RuntimeError("Execution queue is full")
        if prior and prior not in self.external:
            ident = prior
        else:
            if prior:
                self.external.discard(prior)
            ident = str(uuid.uuid4())
            context = arguments.get("execution_context", {})
            data = {
                "model": arguments.get("model", service.default_model),
                "effort": arguments.get("effort", service.default_effort),
                "session_id": session,
                "source": context.get("source", "session"),
                "reference": context.get("reference"),
                "work_area": context.get("work_area"),
            }
            service.records.create(ident, service.owner(), ident, data)
            service.sessions[session] = ident
            self.external.add(ident)
        signal = asyncio.Event()
        self.waiting[ident] = signal
        service.records.update(ident, "queued")
        acquire = asyncio.create_task(self.slots.acquire())
        cancelled = asyncio.create_task(signal.wait())
        dispatched = False
        acquired = False
        rejected = False
        try:
            await asyncio.wait((acquire, cancelled), return_when=asyncio.FIRST_COMPLETED)
            acquired = acquire.done() and not acquire.cancelled() and acquire.exception() is None
            if signal.is_set() or service.stopping or service.records.get(ident)["state"] in TERMINAL:
                raise RuntimeError("Execution cancelled before dispatch")
            if not acquired:
                raise RuntimeError("Execution admission was interrupted")
            self.leases.add(ident)
            self.waiting.pop(ident, None)
            service.records.update(ident, "dispatching")
            dispatched = True
            self.dispatching.add(ident)
            result = await dispatch()
            service.records.update(ident, turn_id=result["run"]["id"])
            return {**result, "execution_id": ident}
        except BaseException as exc:
            from assistant_runtime.api.models import CapabilityRequestError

            if isinstance(exc, CapabilityRequestError):
                rejected = True
                if ident in self.leases:
                    self.release(ident)
                dispatched = False
            service.records.update(
                ident,
                "unknown" if dispatched else "interrupted",
                error={
                    "code": exc.code
                    if isinstance(exc, CapabilityRequestError)
                    else "dispatch_unconfirmed"
                    if dispatched
                    else "dispatch_interrupted"
                },
            )
            if dispatched:
                try:
                    await service.client.request("run.cancel", {"session_id": session}, timeout=20)
                except BaseException:
                    pass  # Keep the slot quarantined until confirmed session closure or shutdown.
                else:
                    self.release(ident)
            else:
                self.release(ident)
            raise
        finally:
            self.dispatching.discard(ident)
            self.waiting.pop(ident, None)
            for task in (acquire, cancelled):
                if not task.done():
                    task.cancel()
            await asyncio.gather(acquire, cancelled, return_exceptions=True)
            # A permit may have arrived simultaneously with cancellation.
            if not acquired and acquire.done() and not acquire.cancelled() and acquire.exception() is None:
                self.slots.release()
            elif acquired and ident not in self.leases and not dispatched and not rejected:
                self.slots.release()

    async def cancel(self, arguments, dispatch):
        service = self.service
        ident = service.sessions.get(arguments["session_id"])
        if not ident:
            return await dispatch()
        row = service.records.get(ident)
        expected = arguments.get("run_id")
        if expected and row["data"].get("turn_id") not in {None, expected}:
            raise ValueError("Cancellation belongs to another execution attempt")
        if self.cancel_waiting(ident):
            service.records.update(ident, "cancelled")
            return {}
        if row["state"] not in TERMINAL:
            service.records.update(ident, "cancelling")
        try:
            result = await dispatch()
        except BaseException:
            service.records.update(ident, "unknown", error={"code": "cancel_unconfirmed"})
            raise
        self.release(ident)
        return result

    def interrupt(self):
        for signal in self.waiting.values():
            signal.set()
        for ident in list(self.leases):
            self.release(ident)

    def closed(self, session):
        ident = self.service.sessions.get(session)
        if ident:
            self.service.records.update(ident, "unknown", error={"code": "session_closed"})
            self.release(ident)
            if ident in self.external:
                self.external.discard(ident)
                self.service.questions.pop(ident, None)
                self.service.sessions.pop(session, None)
