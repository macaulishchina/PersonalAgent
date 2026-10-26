"""Registered inputs and immutable publications; never arbitrary host path access."""

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class FileRequest:
    action: str
    arguments: dict[str, Any] = field(default_factory=dict)
