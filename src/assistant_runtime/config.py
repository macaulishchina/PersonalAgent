"""Instance configuration. Hardware identities never belong in code defaults."""

from dataclasses import dataclass, field
from pathlib import Path
import os
import tomllib

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
    codex_sandbox: str = "workspace-write"
    approval_policy: str = "never"
    assistant_name: str = "个人助理"
    wake_words: list[str] = field(default_factory=lambda: ["嘟嘟"])
    idle_seconds: int = 60
    source_match: str = ""
    default_room: str = "default"
    minimum_volume: int = 80
    echo_cancel: bool = True
    speakers: dict[str, str] = field(default_factory=dict)
    room_labels: dict[str, str] = field(default_factory=dict)
    model_dir: str = ""
    unit_name: str = "personal-agent.service"
    plugins: list[str] = field(default_factory=lambda: ["pipewire", "keyword", "codex", "memory", "gesture"])
    bindings: dict[str, str] = field(
        default_factory=lambda: {
            "audio": "pipewire",
            "trigger": "keyword",
            "brain": "codex",
            "memory": "memory",
        }
    )
    local_plugins: dict[str, str] = field(default_factory=dict)
    memory_enabled: bool = True
    memory_auto_capture: bool = True

    def __post_init__(self):
        for name in ("codex_model", "codex_reasoning_effort", "realtime_voice"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a nonempty string")
            setattr(self, name, value.strip())

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
        if c.profile == "voice" and (c.default_room not in c.speakers or not c.source_match):
            raise ValueError(
                'Configure source_match and a default output in speakers, or use profile="headless"'
            )
        return c
