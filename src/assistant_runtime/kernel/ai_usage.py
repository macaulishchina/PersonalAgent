"""Provider-neutral usage contracts; identity is assigned by trusted host code."""

from contextvars import ContextVar
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
