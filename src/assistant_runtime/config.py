"""Instance configuration. Hardware identities never belong in code defaults."""

from dataclasses import dataclass, field
from pathlib import Path
import os
import re
import tomllib

from assistant_runtime.assistant.quiet import QuietHours

ROOT = Path(__file__).resolve().parents[2]


def default_root():
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "personal-agent"


@dataclass
class Config:
    root: Path = field(default_factory=default_root)
    profile: str = "voice"
    backend: str = "native"
    realtime_voice: str = "juniper"
    workspace: str = field(default_factory=lambda: str(Path.cwd()))
    codex_binary: str = "codex"
    codex_model: str = "gpt-6.1-sol"
    codex_reasoning_effort: str = "high"
    memory_model: str = "gpt-6-luna"
    memory_reasoning_effort: str = "low"
    codex_sandbox: str = "workspace-write"
    approval_policy: str = "never"
    assistant_name: str = "个人助理"
    wake_words: list[str] = field(default_factory=lambda: ["嘟嘟"])
    idle_seconds: int = 60
    quiet_hours: dict[str, str | bool] = field(default_factory=dict)
    web: dict = field(default_factory=dict)
    apple_home: dict = field(default_factory=dict)
    events: dict = field(default_factory=dict)
    media: dict = field(default_factory=dict)
    notifications: dict = field(default_factory=dict)
    tasks: dict = field(default_factory=dict)
    work_files: dict = field(default_factory=dict)
    input_device: str = ""
    default_output: str = ""
    minimum_volume: int = 80
    echo_cancel: bool = True
    devices: dict[str, dict] = field(default_factory=dict)
    model_dir: str = ""
    unit_name: str = "personal-agent.service"
    plugins: list[str] = field(
        default_factory=lambda: [
            "pipewire",
            "keyword",
            "codex",
            "memory",
            "gesture",
            "notifications",
            "integrations",
            "tasks",
        ]
    )
    bindings: dict[str, str] = field(
        default_factory=lambda: {
            "audio": "pipewire",
            "trigger": "keyword",
            "brain": "codex",
            "memory": "memory",
        }
    )
    local_plugins: dict[str, str] = field(default_factory=dict)
    external_mcp: dict = field(default_factory=dict)
    memory_enabled: bool = True
    memory_auto_capture: bool = True

    def __post_init__(self):
        from assistant_runtime.plugins.integrations.settings import IntegrationSettings

        IntegrationSettings.parse(self.external_mcp)
        from assistant_runtime.plugins.web.settings import WebSettings

        from assistant_runtime.plugins.apple_home.settings import AppleHomeSettings

        from assistant_runtime.plugins.tasks.settings import TaskSettings

        from assistant_runtime.plugins.notifications.settings import NotificationSettings

        from assistant_runtime.plugins.events.settings import EventSettings
        from assistant_runtime.plugins.media.settings import MediaSettings

        MediaSettings.parse(self.media)
        EventSettings.parse(self.events)
        NotificationSettings.parse(self.notifications)
        TaskSettings.parse(self.tasks)
        WebSettings.parse(self.web)
        AppleHomeSettings.parse(self.apple_home)
        QuietHours.from_settings(self.quiet_hours)
        for name in (
            "codex_model",
            "codex_reasoning_effort",
            "memory_model",
            "memory_reasoning_effort",
            "realtime_voice",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a nonempty string")
            setattr(self, name, value.strip())

        if not isinstance(self.devices, dict) or len(self.devices) > 32:
            raise ValueError("devices must be a table with at most 32 entries")
        for ident, spec in self.devices.items():
            if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", ident) or not isinstance(spec, dict):
                raise ValueError("Invalid device ID or definition")
            allowed = {
                "adapter",
                "kind",
                "label",
                "location",
                "connection",
                "enabled",
                "max_age_seconds",
                "physical_id",
            }
            if set(spec) - allowed:
                raise ValueError(f"Unknown device settings: {ident}")
            for name in ("adapter", "kind", "label"):
                if not isinstance(spec.get(name), str) or not spec[name].strip() or len(spec[name]) > 120:
                    raise ValueError(f"Invalid device {name}: {ident}")
            physical = spec.get("physical_id", ident)
            if not isinstance(physical, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", physical):
                raise ValueError("Invalid physical device association")
            if not isinstance(spec.get("connection"), dict):
                raise ValueError(f"Device connection must be a table: {ident}")
            if not isinstance(spec.get("location", ""), str) or len(spec.get("location", "")) > 120:
                raise ValueError("Invalid device location")
            if type(spec.get("enabled", True)) is not bool:
                raise ValueError("Device enabled must be boolean")
            age = spec.get("max_age_seconds", 15)
            if type(age) not in (int, float) or not 0 < age <= 3600:
                raise ValueError("Invalid device freshness period")
            if spec.get("enabled", True) and spec["adapter"] not in self.plugins:
                raise ValueError(f"Enabled device adapter not in plugins: {ident}")
        if (
            not isinstance(self.wake_words, list)
            or not self.wake_words
            or any(not isinstance(word, str) or not word.strip() for word in self.wake_words)
        ):
            raise ValueError("wake_words must be a nonempty list of nonempty strings")
        self.wake_words = [word.strip() for word in self.wake_words]
        if len(set(self.wake_words)) != len(self.wake_words):
            raise ValueError("wake_words must not contain duplicates")

    @property
    def output_devices(self):
        return {i: s for i, s in self.devices.items() if s["kind"] == "speaker" and s.get("enabled", True)}

    @property
    def state_dir(self):
        return self.root / "state"

    @property
    def models(self):
        return Path(self.model_dir).expanduser() if self.model_dir else self.root / "models"

    @property
    def socket(self):
        return self.state_dir / "control.sock"

    @classmethod
    def load(cls, path=None):
        configured = path or os.environ.get("PERSONAL_AGENT_CONFIG")
        p = (
            Path(configured).expanduser()
            if configured
            else Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
            / "personal-agent/config.toml"
        )
        data = tomllib.loads(p.read_text()) if p.exists() else {}
        unknown = set(data) - cls.__dataclass_fields__.keys()
        if unknown:
            raise ValueError(f"Unknown config keys: {sorted(unknown)}")
        if not p.exists() and not configured:
            data["profile"] = "headless"
        # Relative instance data stays next to its explicit configuration.
        root = Path(data.pop("root", p.parent if p.exists() else default_root())).expanduser().resolve()
        c = cls(root=root, **data)
        if c.profile not in {"voice", "headless"} or c.backend != "native":
            raise ValueError("Unsupported profile or backend")
        if not 0 <= c.minimum_volume <= 100 or c.idle_seconds < 1:
            raise ValueError("Invalid volume, wake word or idle timeout")
        if c.profile == "voice":
            for ident, kind in ((c.input_device, "microphone"), (c.default_output, "speaker")):
                spec = c.devices.get(ident, {})
                if spec.get("kind") != kind or not spec.get("enabled", True):
                    raise ValueError(f"Configure an enabled {kind} device: {ident}")
        return c
