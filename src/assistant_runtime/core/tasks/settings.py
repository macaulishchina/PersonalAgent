"""Task configuration; main-conversation defaults are resolved by the task owner."""

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

    def validate_bindings(self, entries):
        """Shared static check for saved defaults/recommendations, including dormant ones."""
        from assistant_runtime.api.configuration import ConfigurationError

        rows = [
            ("execution", self.execution),
            *[
                ("model_policy.recommendations." + ident, row)
                for ident, row in ModelPolicy.parse(self.model_policy).recommendations.items()
            ],
        ]
        for path, row in rows:
            model = row.get("model", "auto")
            if model == "auto":
                continue
            prefix = ["tasks", *path.split(".")]
            if model not in entries:
                raise ConfigurationError([*prefix, "model"], "reference", "请选择模型库中已有的模型")
            effort = row.get("reasoning_effort", "auto")
            if effort != "auto" and effort not in entries[model]["reasoning_efforts"]:
                raise ConfigurationError(
                    [*prefix, "reasoning_effort"], "unsupported", "此模型不支持所选思考深度"
                )
