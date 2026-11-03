"""Per-attempt durable permits for Pi's native request retry helper."""

import logging
import uuid
from .native_telemetry import telemetry


class AttemptMeter:
    def __init__(
        self,
        usage,
        scope,
        *,
        service,
        provider,
        model,
        bounds,
        body,
        details=None,
        compaction=False,
        on_ticket=None,
        on_settle=None,
    ):
        self.usage, self.scope = usage, scope
        self.dimensions = dict(
            service=service, provider=provider, model=model, bounds=bounds, body=body, compaction=compaction
        )
        self.details = details or {}
        self.logical_id = uuid.uuid4().hex
        self.on_ticket, self.on_settle = on_ticket, on_settle
        self.ticket = None
        self.state, self.status, self.request_id = "not_sent", None, None
        self.attempts = []

    async def gate(self, event):
        if event["type"] == "attempt_start":
            if self.ticket is not None:
                raise RuntimeError("Previous model attempt not settled")
            self.ticket = await self.usage.admit(
                self.scope,
                **self.dimensions,
                details={
                    **self.details,
                    "logical_request_id": self.logical_id,
                    "attempt": event["attempt"],
                    "retry_wait_ms": event.get("retry_wait_ms", 0),
                },
            )
            self.state, self.status, self.request_id = "not_sent", None, None
            self.retry_wait_ms = event.get("retry_wait_ms", 0)
            if self.on_ticket:
                self.on_ticket(self.ticket)
            return {"call_id": self.ticket["id"]}
        terminal = event["terminal"]
        failure = event.get("failure")
        message = terminal.get("message") if terminal.get("type") == "done" else terminal.get("error")
        usage = telemetry(message or {})
        status = (failure or {}).get("status", self.status)
        self.request_id = (
            (failure or {}).get("request_id") or self.request_id or (message or {}).get("responseId")
        )
        state = (
            "completed"
            if terminal["type"] == "done"
            else (
                "not_sent"
                if failure and failure.get("submission") == "not_submitted"
                else "rejected"
                if status and status >= 400
                else "unknown"
            )
        )
        record = await self.finish(
            state=state,
            usage=usage,
            status=status,
            failure=failure,
            generation_settled=event.get("generation_settled") is True,
        )
        self.attempts.append(
            {
                "call_id": record["id"],
                "state": state,
                "attempt": event["attempt"],
                "retry_wait_ms": self.retry_wait_ms,
            }
        )
        return {"settled": True}

    def observe(self, event):
        if event["type"] == "dispatch":
            # Preparation alone does not establish submission. IPC loss afterwards
            # remains conservative because sending can occur before the next event.
            self.state = "unknown"
        elif event["type"] == "response":
            self.state = "unknown"
            self.status, self.request_id = event["status"], event.get("request_id")

    async def finish(self, *, state=None, usage=None, status=None, failure=None, generation_settled=False):
        if not self.ticket:
            return None
        record = await self.usage.settle(
            self.ticket["id"],
            state=state or self.state,
            usage=usage,
            http_status=status or self.status,
            provider_request_id=self.request_id,
            error=(failure or {}).get("code"),
            generation_settled=generation_settled,
        )
        if self.on_settle:
            self.on_settle(self.ticket, record, generation_settled)
        logging.getLogger(__name__).info(
            "model_attempt logical=%s call=%s state=%s category=%s",
            self.logical_id,
            record["id"],
            record["state"],
            (failure or {}).get("category"),
        )
        self.ticket = None
        return record
