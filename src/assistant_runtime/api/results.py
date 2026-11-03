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


class ResultPresentation(TypedDict, total=False):
    """Optional tool details['personal_agent.presentation']; never an authorization grant."""

    summary: str
    upstream_complete: bool | None
