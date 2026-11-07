"""Provider-neutral, memory-only speech boundaries; no transport or model policy."""

import re
from dataclasses import dataclass
from typing import Protocol
from typing import AsyncIterator
from .ai_usage import UsageScope


class SpeechError(RuntimeError):
    """Safe public error; never contains provider bodies or credentials."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class RecognitionStream(Protocol):
    async def send(self, pcm: bytes) -> None: ...
    async def finish(self) -> str: ...
    async def close(self) -> None: ...


@dataclass(frozen=True)
class RecognitionRequest:
    """Provider contract, independently bound from synthesis."""

    scope: UsageScope
    max_seconds: int = 60
    action = "recognize"


@dataclass(frozen=True)
class SynthesisRequest:
    scope: UsageScope
    text: str
    action = "synthesize"


@dataclass(frozen=True)
class SpeechRequest:
    action: str
    scope: UsageScope
    text: str = ""


class PlaybackStream(Protocol):
    def __aiter__(self) -> AsyncIterator[bytes]: ...
    async def aclose(self) -> None: ...


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


def spoken_excerpt(text, offset=0, limit=200):
    """Return a complete prefix and its source offset; never split an unfinished sentence."""
    if not isinstance(text, str) or type(offset) is not int or not 0 <= offset <= len(text):
        raise SpeechError("speech_invalid_text")
    rest = text[offset:]
    if len(rest) <= limit:
        return rest, len(text)
    boundaries = list(re.finditer(r"[。！？!?\n]|\.(?=\s)", rest[:limit]))
    if not boundaries:
        return "", offset
    end = offset + boundaries[-1].end()
    return text[offset:end], end
