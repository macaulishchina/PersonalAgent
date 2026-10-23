"""Provider-neutral usage contracts; identity is assigned by trusted host code."""

from contextvars import ContextVar
import re
from dataclasses import dataclass, field
from typing import Any


class UsageError(RuntimeError):
    def __init__(self, code: str, work_id: str | None = None):
        super().__init__(code)
        self.code, self.work_id = code, work_id


@dataclass(frozen=True)
class UsageScope:
    work_id: str
    caller: str
    task_id: str = ""
    parent_call: str = ""
    execution_version: str = ""


current_usage: ContextVar[UsageScope | None] = ContextVar("ai_usage", default=None)


@dataclass(frozen=True)
class UsageRequest:
    action: str
    arguments: dict[str, Any] = field(default_factory=dict)
    caller_run: str | None = None


async def metered(usage, scope, service, provider, model, invoke, *, bounds=None, body=None):
    """For one already-validated no-tool request, including synthetic adapters."""
    details = {}
    if isinstance(body, dict):
        for key in ("model", "max_output_tokens"):
            if key in body:
                details["model_alias" if key == "model" else key] = body[key]
        if isinstance(body.get("reasoning"), dict) and "effort" in body["reasoning"]:
            details["reasoning_effort"] = body["reasoning"]["effort"]
    ticket = await usage.admit(
        scope,
        service=service,
        provider=provider,
        model=model,
        bounds=bounds,
        body=body,
        details=details,
    )
    try:
        result = await invoke()
    except BaseException as exc:
        status = None
        match = re.fullmatch(r"model_http_(\d{3})", str(exc))
        if match:
            status = int(match[1])
        await usage.settle(
            ticket["id"],
            state="rejected" if status else "unknown",
            http_status=status,
            error=getattr(exc, "code", type(exc).__name__),
        )
        raise
    result["usage_call_id"] = ticket["id"]
    await usage.settle(
        ticket["id"],
        state="completed",
        usage=result.get("usage"),
        provider_request_id=result.get("provider_request_id"),
        response_body=result,
    )
    return result
