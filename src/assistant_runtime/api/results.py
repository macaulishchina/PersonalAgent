"""Content delivery evidence; independent from execution/business outcomes."""

from typing import Any, Literal, TypedDict


class ResultReference(TypedDict):
    id: str
    execution_id: str
    part: str
    version: str
    state: Literal["ready", "expired", "unavailable"]
    bytes: int
    complete_received: bool
    upstream_complete: bool | None
    attachments: list[dict[str, Any]]


class ResultAccessError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)

    def tool_failure(self):
        from .tool_calls import ToolFailure, ToolOutcome

        correctable = self.code in {
            "result_path_missing",
            "result_invalid_path",
            "result_invalid_arguments",
            "result_invalid_format",
            "result_invalid_range",
        }
        return ToolFailure(
            ToolOutcome(
                self.code,
                "result",
                "dispatched",
                "failed",
                "读取结果失败：" + self.code + "。原工具不会因此重新执行。",
                field="path" if "path" in self.code else None,
                next_action="correct_arguments" if correctable else "inspect_result_reference",
                retryable=False,
            )
        )


class ResultPresentation(TypedDict, total=False):
    """Optional tool details['personal_agent.presentation']; never an authorization grant."""

    summary: str
    upstream_complete: bool | None
