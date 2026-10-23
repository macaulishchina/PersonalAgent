"""Public single-execution operations; caller identity is supplied by the host."""

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ExecutionRequest:
    action: str
    arguments: dict[str, Any] = field(default_factory=dict)
