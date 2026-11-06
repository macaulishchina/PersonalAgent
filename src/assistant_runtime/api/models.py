"""Provider-neutral requests; adapters own credentials, protocols and continuations."""

from dataclasses import dataclass, field
from typing import Any, TypedDict
from .tool_calls import ToolFailure, ToolOutcome


class ModelFailure(TypedDict, total=False):
    """Safe diagnostic evidence, shared by execution and operator presentation."""

    code: str
    category: str
    message: str
    status: int
    native_error: str
    error_type: str
    cause_code: str
    cause_message: str
    transport_code: str
    last_event: str
    terminal_event: str
    sdk_terminal: str
    response_id: str
    provider_code: str
    request_id: str
    phase: str
    submission: str
    output_started: bool
    generation_settled: bool
    retry_allowed: bool
    attempt_count: int
    attempts: list[dict[str, Any]]
    stop_reason: str


class ModelCallError(RuntimeError):
    """Safe model failure with evidence; uncertain operations must not replay."""

    def __init__(self, code: str, failure: ModelFailure | None = None):
        super().__init__(code)
        self.code = code
        self.failure = failure or {}


class CapabilityRequestError(ToolFailure):
    """Safe, owner-confirmed rejection before an external model dispatch."""

    dispatch_state = "not_dispatched"

    def __init__(self, code: str, message: str):
        super().__init__(ToolOutcome(code, "validation", "not_dispatched", "failed", message))
        self.code = code


def expected_model(value):
    """Validate a public route condition without accepting private configuration."""
    import re

    if (
        not isinstance(value, dict)
        or set(value) != {"model", "revision"}
        or not isinstance(value["model"], str)
        or not 1 <= len(value["model"]) <= 64
        or not isinstance(value["revision"], str)
        or not re.fullmatch(r"[a-f0-9]{64}", value["revision"])
    ):
        raise CapabilityRequestError("invalid_arguments", "Expected a public model and route revision")
    return dict(value)


@dataclass(frozen=True)
class ModelRequest:
    action: str
    arguments: dict[str, Any] = field(default_factory=dict)
    caller_run: str | None = None
