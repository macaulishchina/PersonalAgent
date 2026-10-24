"""Public, provider-neutral conversations; callers never supply engine identities."""

from dataclasses import dataclass, field
from contextvars import ContextVar

current_conversation = ContextVar("current_conversation", default=None)


class ConversationCancelled(ValueError):
    """Input was cancelled before dispatch; no model request was sent."""


@dataclass(frozen=True)
class ConversationRequest:
    action: str
    arguments: dict = field(default_factory=dict)
