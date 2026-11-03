"""Provider-neutral requests; adapters own credentials, protocols and continuations."""

from dataclasses import dataclass, field
from typing import Any, TypedDict


class ModelFailure(TypedDict, total=False):
    """Safe diagnostic evidence, shared by execution and operator presentation."""

    code: str
    category: str
    message: str
    status: int
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


@dataclass(frozen=True)
class ModelRequest:
    action: str
    arguments: dict[str, Any] = field(default_factory=dict)
    caller_run: str | None = None
