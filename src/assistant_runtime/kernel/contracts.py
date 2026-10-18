"""Version 1 boundary values: commands request work, events report facts."""

from dataclasses import dataclass, field
from typing import Any, Protocol
from pathlib import Path
import time
import uuid
import math

API_VERSION = 1


@dataclass(frozen=True)
class DecisionSample:
    """Ephemeral shadow input. No result may authorize or dispatch work."""

    kind: str
    reference: str
    state: dict[str, Any]
    baseline: str | None = None
    observed_at: float = field(default_factory=time.time)


@dataclass(frozen=True)
class DecisionRequest:
    """Local decision management; never a device/action interface."""

    action: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DecisionQuery:
    """A bounded suggestion. Authorization and effect verification remain with callers."""

    kind: str
    reference: str
    state: dict[str, Any]
    caller_run: str | None = None


@dataclass(frozen=True)
class MediaRequest:
    action: str
    arguments: dict[str, Any] = field(default_factory=dict)
    caller_run: str | None = None


@dataclass(frozen=True)
class IntegrationRequest:
    action: str
    arguments: dict[str, Any] = field(default_factory=dict)
    caller_run: str | None = None


class IntegrationProvider(Protocol):
    """Optional reasoning-provider seam; no native RPC fields escape the adapter."""

    def status(self) -> dict: ...
    async def apply(self, force: bool = False) -> dict: ...
    async def verify(self, name: str, context: str, schemas: bool = False) -> dict: ...
    async def login(self, name: str) -> dict: ...


@dataclass(frozen=True)
class NotificationRequest:
    action: str
    arguments: dict[str, Any] = field(default_factory=dict)
    caller_run: str | None = None


@dataclass(frozen=True)
class TaskRequest:
    action: str
    arguments: dict[str, Any] = field(default_factory=dict)
    caller_run: str | None = None


@dataclass(frozen=True)
class VoiceBinding:
    """The process-lifetime voice endpoints, separate from device tool operations."""

    input_device: str
    output_device: str


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


def operation_id():
    return uuid.uuid4().hex


def validate_operation_id(value):
    if not isinstance(value, str) or len(value) != 32 or any(c not in "0123456789abcdef" for c in value):
        raise ValueError("Operation ID must be 32 lowercase hexadecimal characters")


@dataclass(frozen=True)
class DeviceOperationQuery:
    device_id: str
    operation_id: str | None = None

    def __post_init__(self):
        if self.operation_id is not None:
            validate_operation_id(self.operation_id)


@dataclass(frozen=True)
class DeviceOperationCancel:
    device_id: str
    operation_id: str

    def __post_init__(self):
        validate_operation_id(self.operation_id)


@dataclass(frozen=True)
class AudioVolume:
    device_id: str
    percent: int
    operation_id: str = field(default_factory=operation_id, compare=False)

    def __post_init__(self):
        validate_operation_id(self.operation_id)
        if type(self.percent) is not int or not 0 <= self.percent <= 100:
            raise ValueError("Volume must be an integer from 0 to 100")


@dataclass(frozen=True)
class VideoCapture:
    device_id: str
    operation_id: str = field(default_factory=operation_id, compare=False)

    def __post_init__(self):
        validate_operation_id(self.operation_id)


MAX_VIDEO_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True)
class VideoRecord:
    device_id: str
    duration_seconds: int = 10
    operation_id: str = field(default_factory=operation_id, compare=False)

    def __post_init__(self):
        validate_operation_id(self.operation_id)
        if not isinstance(self.device_id, str) or not 0 < len(self.device_id) <= 64:
            raise ValueError("Invalid camera device ID")
        if type(self.duration_seconds) is not int or not 1 <= self.duration_seconds <= 60:
            raise ValueError("Video duration must be between 1 and 60 seconds")


@dataclass(frozen=True)
class VideoClip:
    """A completed local MP4; large media never traverses the capability socket."""

    device_id: str
    path: Path
    width: int
    height: int
    duration_seconds: float
    byte_count: int
    recorded_at: float
    generation: int
    mime_type: str = "video/mp4"

    def __post_init__(self):
        if (
            not self.device_id
            or not isinstance(self.path, Path)
            or not self.path.is_absolute()
            or self.mime_type != "video/mp4"
            or not 0 < self.byte_count <= MAX_VIDEO_BYTES
            or not 0 < self.width <= 1920
            or not 0 < self.height <= 1080
            or not math.isfinite(self.duration_seconds)
            or not 0 < self.duration_seconds <= 61
            or not math.isfinite(self.recorded_at)
            or self.generation < 1
        ):
            raise ValueError("Invalid recorded video")


@dataclass(frozen=True)
class CameraMotion:
    """A bounded directional movement or explicit stop; no angle assumptions."""

    device_id: str
    direction: str
    duration_ms: int = 80
    operation_id: str = field(default_factory=operation_id, compare=False)

    def __post_init__(self):
        validate_operation_id(self.operation_id)
        if not isinstance(self.device_id, str) or not 0 < len(self.device_id) <= 64:
            raise ValueError("Invalid camera device ID")
        if not isinstance(self.direction, str) or self.direction not in {
            "left",
            "right",
            "up",
            "down",
            "stop",
        }:
            raise ValueError("Invalid camera movement direction")
        if type(self.duration_ms) is not int or not 0 <= self.duration_ms <= 500:
            raise ValueError("Camera movement duration must be bounded to 500 ms")
        if (self.direction == "stop") != (self.duration_ms == 0):
            raise ValueError("Stop requires zero duration; movement requires positive duration")


@dataclass(frozen=True)
class VideoFrame:
    """One bounded, fresh JPEG; pixels never belong on the event bus."""

    device_id: str
    data: bytes
    width: int
    height: int
    captured_at: float
    generation: int
    mime_type: str = "image/jpeg"

    def __post_init__(self):
        if (
            not self.device_id
            or self.mime_type != "image/jpeg"
            or not 0 < len(self.data) <= 1024 * 1024
            or not 0 < self.width <= 1920
            or not 0 < self.height <= 1080
            or not math.isfinite(self.captured_at)
            or self.generation < 1
        ):
            raise ValueError("Invalid or oversized video frame")


class VideoInputProvider(Protocol):
    async def capture(self) -> VideoFrame: ...
    async def record(self, duration_seconds: int) -> VideoClip: ...
    async def close(self) -> None: ...


@dataclass(frozen=True)
class SensorReading:
    """One metric's validity and source time, independent of device heartbeats."""

    metric: str
    value: float | None
    unit: str
    source: str
    observed_at: float | None = None
    status: str = "valid"

    def __post_init__(self):
        if (
            not isinstance(self.metric, str)
            or not self.metric
            or len(self.metric) > 64
            or not isinstance(self.unit, str)
            or not self.unit
            or len(self.unit) > 32
        ):
            raise ValueError("Invalid sensor metric/unit")
        if not isinstance(self.source, str) or not self.source or len(self.source) > 200:
            raise ValueError("Invalid sensor source")
        if not isinstance(self.status, str) or self.status not in {
            "valid",
            "unavailable",
            "calibrating",
            "error",
        }:
            raise ValueError("Invalid sensor reading status")
        if self.observed_at is not None and (
            type(self.observed_at) not in (int, float)
            or not math.isfinite(self.observed_at)
            or self.observed_at < 0
        ):
            raise ValueError("Invalid sensor observation time")
        if self.status == "valid":
            if (
                self.value is None
                or type(self.value) not in (float, int)
                or not math.isfinite(self.value)
                or self.observed_at is None
            ):
                raise ValueError("Valid sensor reading requires a finite value and observation time")
        elif self.value is not None:
            raise ValueError("Unavailable sensor reading cannot contain a current value")


@dataclass(frozen=True)
class DeviceObservation:
    state: str
    data: dict[str, Any] = field(default_factory=dict)
    observed_at: float = field(default_factory=time.time)
    error: str | None = None
    readings: dict[str, SensorReading] = field(default_factory=dict)


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


class CaptureTransport(Protocol):
    """Independent PCM16 capture; closing it never closes another consumer."""

    @property
    def healthy(self) -> bool: ...
    async def read(self, size: int) -> bytes: ...
    async def close(self) -> None: ...


class AudioInputProvider(Protocol):
    async def open(self, sample_rate: int) -> CaptureTransport: ...


class AudioOutputProvider(Protocol):
    """A registered speaker provider; unknown volume stays None."""

    volume: int | None

    async def ensure_ready(self) -> None: ...
    async def open(self, sample_rate: int) -> PlaybackTransport: ...
    async def set_volume(self, value: int) -> dict: ...
    async def close(self) -> None: ...


class MemoryProvider(Protocol):
    async def start(self) -> None: ...
    async def search(self, query: str, scope: str) -> list[dict]: ...
    async def read(self, ident: str) -> dict | None: ...
    async def upsert(self, record: dict) -> None: ...
    async def delete(self, ident: str) -> None: ...
    async def export(self) -> list[dict]: ...
    async def import_records(self, records: list[dict]) -> None: ...
    async def capture(self, sources: list[dict], scope: str, project: str, job_id: str) -> dict: ...
    async def close(self) -> None: ...


class VoiceSession(Protocol):
    """16 kHz mono PCM16 in; AudioFrame output and normalized events via callbacks."""

    epoch: int
    memory_context: str
    last_error: str | None
    # Completion of the current spoken turn, not proof of acoustic delivery.
    # Reset when opening a session or submitting new speech.
    speech_completed: bool

    async def open(self, preroll=(), *, notification_only: bool = False) -> None: ...
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
    async def memory_complete(self, messages: list[dict], schema: dict) -> dict: ...


class BrainPlugin(Protocol):
    backend: TaskEngine
    voice: VoiceSession

    def diagnostics(self) -> dict: ...


@dataclass(frozen=True)
class EventRequest:
    action: str
    arguments: dict = field(default_factory=dict)
    caller_run: str | None = None


@dataclass(frozen=True)
class ObservationRequest:
    """Source lifecycle/configuration, separate from durable record operations."""

    action: str
    arguments: dict = field(default_factory=dict)
    caller_run: str | None = None
