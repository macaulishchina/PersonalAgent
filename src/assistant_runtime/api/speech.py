"""Provider-neutral, memory-only speech boundaries; no transport or model policy."""

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class InputChunk:
    sequence: int
    started: float
    ended: float
    pcm: bytes


@dataclass(frozen=True)
class Recognition:
    text: str
    final: bool = False
    speech_ended_at: float | None = None


class StreamingRecognition(Protocol):
    async def reset(self) -> None: ...
    async def accept(self, chunk: InputChunk) -> Recognition: ...
    async def close(self) -> None: ...


class SpeechRenderer(Protocol):
    """Literal speech, bounded mono PCM16 at 24 kHz; never executes user requests."""

    async def render(self, text: str) -> bytes: ...
    async def close(self) -> None: ...


@dataclass(frozen=True)
class RequestRef:
    id: str
    revision: int


@dataclass(frozen=True)
class FeedbackIntent:
    request: RequestRef
    kind: str
    text: str
    expires_at: float
    perceptible: bool = False
    important: bool = False
