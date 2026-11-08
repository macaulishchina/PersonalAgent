"""Public, provider-neutral conversations; callers never supply engine identities."""

from dataclasses import dataclass, field
from contextvars import ContextVar

current_conversation = ContextVar("current_conversation", default=None)


class ConversationViews:
    """Trusted entrance factory; callers authorize a session before opening its view.

    Opening a view never changes another entrance's default binding. Returned
    handles scope history, uploads, attachments and controls to that one session.
    """

    def __init__(self, opener):
        self.__opener = opener

    async def open(self, session_id):
        return await self.__opener(session_id)


class ConversationCancelled(ValueError):
    """Input was cancelled before dispatch; no model request was sent."""


class ConversationInputChanged(ValueError):
    """Target changed before any input was dispatched; caller may re-evaluate routing."""


@dataclass(frozen=True)
class ConversationRequest:
    action: str
    arguments: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ConversationInputRequest:
    """Durable entrance input reservation, including pre-transcription work identity."""

    action: str
    arguments: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ConversationAccessRequest:
    """Trusted adapter approval of Web access, never a browser/model operation.

    The adapter derives endpoint_id from an authenticated device, not client
    arguments. Web binds each entrance to that endpoint and owns request/grant
    records. No action accepts an arbitrary conversation identity.
    """

    action: str
    endpoint_id: str
    arguments: dict = field(default_factory=dict)
