from dataclasses import dataclass, field
from typing import Any
import time
import uuid

API_VERSION = 1


@dataclass(frozen=True)
class Event:
    kind: str
    source: str
    data: dict[str, Any] = field(default_factory=dict)
    correlation_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    time: float = field(default_factory=time.time)
    generation: int = 0
