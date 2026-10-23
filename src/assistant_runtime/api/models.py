"""Provider-neutral requests; adapters own credentials, protocols and continuations."""

from dataclasses import dataclass, field
from typing import Any


class ModelCallError(RuntimeError):
    """Sanitized adapter failure; only explicit rejection permits automatic fallback."""

    def __init__(self, code: str, *, fallback_allowed: bool = False):
        super().__init__(code)
        self.code = code
        self.fallback_allowed = fallback_allowed


@dataclass(frozen=True)
class ModelRequest:
    action: str
    arguments: dict[str, Any] = field(default_factory=dict)
    caller_run: str | None = None
