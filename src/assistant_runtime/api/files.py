"""Registered input resources; bytes are supplied, never arbitrary host paths."""

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class FileRequest:
    action: str
    arguments: dict[str, Any] = field(default_factory=dict)
