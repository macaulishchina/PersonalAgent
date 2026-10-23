"""Strict instance settings, independent from foreground and memory models."""

from assistant_runtime.api.configuration import configuration_default

from dataclasses import dataclass, field
from zoneinfo import ZoneInfo
from assistant_runtime.core.tasks.selection import ModelPolicy


@dataclass
class TaskSettings:
    timezone: str = field(default_factory=lambda: configuration_default(__file__, "timezone"))
    concurrency: int = field(default_factory=lambda: configuration_default(__file__, "concurrency"))
    misfire_seconds: int = field(default_factory=lambda: configuration_default(__file__, "misfire_seconds"))
    max_goal_rounds: int = field(default_factory=lambda: configuration_default(__file__, "max_goal_rounds"))
    execution: dict = field(default_factory=lambda: configuration_default(__file__, "execution"))
    model_policy: dict = field(default_factory=lambda: configuration_default(__file__, "model_policy"))

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
        result.execution = {**configuration_default(__file__, "execution"), **result.execution}
        if any(not isinstance(v, str) or not v.strip() for v in result.execution.values()):
            raise ValueError("Task execution settings must be nonempty strings")
        ModelPolicy.parse(result.model_policy)
        return result
