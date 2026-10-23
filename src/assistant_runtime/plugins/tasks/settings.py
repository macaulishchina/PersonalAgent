"""Strict instance settings, independent from foreground and memory models."""

from dataclasses import dataclass, field
from zoneinfo import ZoneInfo
from .selection import ModelPolicy


@dataclass
class TaskSettings:
    timezone: str = "Asia/Shanghai"
    concurrency: int = 2
    misfire_seconds: int = 300
    max_goal_rounds: int = 8
    execution: dict = field(default_factory=lambda: {"model": "auto", "reasoning_effort": "auto"})
    model_policy: dict = field(default_factory=dict)

    @classmethod
    def parse(cls, data):
        if not isinstance(data, dict) or set(data) - cls.__dataclass_fields__.keys():
            raise ValueError("Unknown task settings")
        result = cls(**data)
        ZoneInfo(result.timezone)
        if type(result.concurrency) is not int or not 1 <= result.concurrency <= 8:
            raise ValueError("Task concurrency must be 1..8")
        if type(result.max_goal_rounds) is not int or not 1 <= result.max_goal_rounds <= 64:
            raise ValueError("Task goal rounds must be 1..64")
        if type(result.misfire_seconds) is not int or not 0 <= result.misfire_seconds <= 86400:
            raise ValueError("Invalid task misfire window")
        if not isinstance(result.execution, dict) or set(result.execution) - {"model", "reasoning_effort"}:
            raise ValueError("Unknown task execution settings")
        result.execution = {"model": "auto", "reasoning_effort": "auto", **result.execution}
        if any(not isinstance(v, str) or not v.strip() for v in result.execution.values()):
            raise ValueError("Task execution settings must be nonempty strings")
        ModelPolicy.parse(result.model_policy)
        return result
