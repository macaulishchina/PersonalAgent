"""Version 1 boundary values: commands request work, events report facts."""

from dataclasses import dataclass, field
from typing import Any, Protocol
import time
import uuid

API_VERSION = 1


@dataclass(frozen=True)
class Control:
    kind: str
    value: Any = None


@dataclass(frozen=True)
class Trigger:
    source: str
    kind: str = "wake"


@dataclass(frozen=True)
class MemoryRequest:
    action: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DeviceQuery:
    action: str = "list"
    device_id: str | None = None


@dataclass(frozen=True)
class DeviceObservation:
    state: str
    data: dict[str, Any] = field(default_factory=dict)
    observed_at: float = field(default_factory=time.time)
    error: str | None = None


@dataclass(frozen=True)
class Event:
    kind: str
    source: str
    data: dict[str, Any] = field(default_factory=dict)
    correlation_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    time: float = field(default_factory=time.time)
    generation: int = 0


@dataclass(frozen=True)
class AudioFrame:
    pcm: bytes
    sample_rate: int
    generation: int
    timestamp: float = field(default_factory=time.monotonic)
    channels: int = 1
    format: str = "s16le"

    def __post_init__(self):
        if (
            self.sample_rate <= 0
            or self.channels < 1
            or self.format != "s16le"
            or len(self.pcm) % (2 * self.channels)
        ):
            raise ValueError("Invalid PCM frame")


class PlaybackTransport(Protocol):
    """A bounded 20ms mono PCM16 writer; close discards queued playback."""

    @property
    def healthy(self) -> bool: ...
    async def write(self, pcm: bytes) -> None: ...
    async def close(self) -> None: ...


class AudioOutputProvider(Protocol):
    """A registered external speaker transport; unknown volume stays None."""

    volume: int | None

    async def ensure_ready(self) -> None: ...
    async def open(self, sample_rate: int) -> PlaybackTransport: ...
    async def set_volume(self, value: int) -> None: ...
    async def close(self) -> None: ...


class MemoryProvider(Protocol):
    async def start(self) -> None: ...
    async def search(self, query: str, scope: str) -> list[dict]: ...
    async def read(self, ident: str) -> dict | None: ...
    async def upsert(self, record: dict) -> None: ...
    async def delete(self, ident: str) -> None: ...
    async def export(self) -> list[dict]: ...
    async def import_records(self, records: list[dict]) -> None: ...
    async def close(self) -> None: ...


class VoiceSession(Protocol):
    """16 kHz mono PCM16 in; AudioFrame output and normalized events via callbacks."""

    epoch: int
    memory_context: str
    last_error: str | None

    async def open(self, preroll=()) -> None: ...
    def feed(self, pcm: bytes) -> None: ...
    async def send_text(self, text: str): ...
    async def instruct(self, text: str) -> None: ...
    async def say(self, text: str) -> None: ...
    async def close(self) -> None: ...


class TaskEngine(Protocol):
    """One foreground task; interruption of speech is not task cancellation."""

    session_id: str
    progress: str
    last_user_text: str
    last_error: str | None

    @property
    def busy(self) -> bool: ...
    async def submit(self, text: str): ...
    async def cancel(self) -> bool: ...
    async def reflect(self, sources: list[dict], existing: list[dict]) -> list[dict]: ...


class BrainPlugin(Protocol):
    backend: TaskEngine
    voice: VoiceSession

    def diagnostics(self) -> dict: ...
