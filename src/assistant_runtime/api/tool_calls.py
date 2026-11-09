"""Provider-neutral tool outcomes. Dispatch and business success are independent."""

from dataclasses import asdict, dataclass
from typing import Any, Literal, TypedDict


@dataclass(frozen=True)
class ToolOutcome:
    code: str
    stage: str
    dispatch: str
    outcome: str
    message: str
    field: str | None = None
    next_action: str | None = None
    receipt: dict[str, Any] | None = None
    retryable: bool | None = None

    def public(self):
        return {key: value for key, value in asdict(self).items() if value is not None}


class ToolFailure(ValueError):
    """An owner-confirmed outcome; never infer non-dispatch from arbitrary exceptions."""

    def __init__(self, outcome: ToolOutcome, *, result=None):
        self.outcome = outcome
        self.result = result
        super().__init__(outcome.message)


def invalid_arguments(message, field=None):
    return ToolFailure(
        ToolOutcome(
            "invalid_arguments",
            "validation",
            "not_dispatched",
            "failed",
            message,
            field=field,
            next_action="correct_arguments",
            retryable=False,
        )
    )


class ToolMediaReceipt(TypedDict):
    """Framework retention/delivery evidence, independent of tool business outcome."""

    state: Literal["ready", "partial", "failed"]
    attachments: list[dict[str, Any]]
    issues: list[dict[str, Any]]
