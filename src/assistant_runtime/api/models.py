"""Provider-neutral requests; adapters own credentials, protocols and continuations."""

from dataclasses import dataclass, field
from typing import Any


class ModelCallError(RuntimeError):
    """Sanitized adapter failure. Model calls are never automatically replayed."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ModelRequest:
    action: str
    arguments: dict[str, Any] = field(default_factory=dict)
    caller_run: str | None = None
