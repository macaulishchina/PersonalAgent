"""Trusted entrance contracts; not model tools and never client-supplied session authority."""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class TerminalNotifications:
    action: str
    session_id: str
    endpoint_id: str
    arguments: dict = field(default_factory=dict)
