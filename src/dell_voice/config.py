from dataclasses import dataclass, field
from pathlib import Path
import os
import tomllib

ROOT = Path(__file__).resolve().parents[2]


@dataclass
class Config:
    root: Path = ROOT
    backend: str = "native"
    realtime_voice: str = "juniper"
    workspace: str = "/home/macaulish/workspace"
    codex_binary: str = "/home/macaulish/.local/bin/codex"
    codex_sandbox: str = "danger-full-access"
    approval_policy: str = "never"
    assistant_name: str = "嘟嘟"
    wake_word: str = "嘟嘟"
    idle_seconds: int = 60
    source_match: str = "UGREEN_Camera_2K"
    default_room: str = "living"
    minimum_volume: int = 80
    echo_cancel: bool = True
    speakers: dict = field(default_factory=lambda: {"living":"4C:C6:4C:97:78:4A", "study":"8C:53:C3:7F:18:7C"})

    @property
    def state_dir(self):
        return self.root / "state"

    @property
    def socket(self):
        return self.state_dir / "control.sock"

    @classmethod
    def load(cls, path=None):
        p = Path(path or os.environ.get("DELL_VOICE_CONFIG", ROOT / "config.toml"))
        data = tomllib.loads(p.read_text()) if p.exists() else {}
        unknown = set(data) - (cls.__dataclass_fields__.keys() - {"root"})
        if unknown:
            raise ValueError(f"Unknown config keys: {sorted(unknown)}")
        c = cls(**data)
        if c.backend != "native":
            raise ValueError("Only native Codex subscription voice is supported")
        if c.default_room not in c.speakers or not 0 <= c.minimum_volume <= 100:
            raise ValueError("Invalid room or volume")
        if c.idle_seconds < 1 or not c.wake_word.strip() or not c.assistant_name.strip():
            raise ValueError("Invalid wake word or idle timeout")
        return c

