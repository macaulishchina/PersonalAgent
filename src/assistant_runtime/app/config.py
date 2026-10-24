"""Instance configuration. Hardware identities never belong in code defaults."""

from assistant_runtime.api.configuration import configuration_default

from dataclasses import dataclass, field
from pathlib import Path
import os
import re
import tomllib


ROOT = Path(__file__).resolve().parents[2]


def default_root():
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "personal-agent"


def _read_config(path, *, text=None):
    """Combine one entry file and its explicit, nonrecursive includes without overrides."""
    path = Path(path).expanduser().resolve()

    def parse(source, content=None):
        try:
            return tomllib.loads(source.read_text(encoding="utf-8") if content is None else content)
        except tomllib.TOMLDecodeError as exc:
            raise ValueError(f"Invalid TOML configuration: {source}") from exc

    data = parse(path, text)
    includes = data.pop("include", [])
    if not isinstance(includes, list) or len(includes) > 32:
        raise ValueError(f"include must be a list of at most 32 files: {path}")
    origins = {}

    def merge(target, incoming, source, prefix=()):
        for key, value in incoming.items():
            location = (*prefix, key)
            if key in target:
                if isinstance(target[key], dict) and isinstance(value, dict):
                    merge(target[key], value, source, location)
                    continue
                raise ValueError(
                    f"Duplicate configuration {'.'.join(location)}: {origins[location]} and {source}"
                )
            origins[location] = source
            if isinstance(value, dict):
                target[key] = {}
                merge(target[key], value, source, location)
            else:
                target[key] = value

    combined = {}
    merge(combined, data, path)
    seen = {path}
    for name in includes:
        if not isinstance(name, str) or not name.strip() or Path(name).is_absolute():
            raise ValueError(f"include entries must be nonempty relative file paths: {path}")
        source = (path.parent / name).resolve()
        if not source.is_relative_to(path.parent):
            raise ValueError(f"Included configuration must stay inside {path.parent}")
        if source in seen:
            raise ValueError(f"Duplicate included configuration: {source}")
        seen.add(source)
        fragment = parse(source)
        if "include" in fragment:
            raise ValueError(f"Only the entry configuration may declare include: {source}")
        merge(combined, fragment, source)
    return combined


def read_config(path, *, text=None):
    from assistant_runtime.api.configuration_io import configuration_lock

    path = Path(path).expanduser().resolve()
    try:
        entry = tomllib.loads(path.read_text(encoding="utf-8") if text is None else text)
    except tomllib.TOMLDecodeError:
        raise ValueError("Invalid entry configuration") from None
    root = Path(entry.get("root", path.parent)).expanduser()
    root = (path.parent / root).resolve() if not root.is_absolute() else root.resolve()
    with configuration_lock(root):
        if (root / "state/configuration/transaction.json").exists():
            raise ValueError(
                "Incomplete configuration transaction; start configuration management to recover"
            )
        return _read_config(path, text=text)


@dataclass
class Config:
    root: Path = field(default_factory=default_root)
    profile: str = field(default_factory=lambda: configuration_default(__file__, "profile"))
    backend: str = field(default_factory=lambda: configuration_default(__file__, "backend"))
    realtime_voice: str = field(default_factory=lambda: configuration_default(__file__, "realtime_voice"))
    workspace: str = field(default_factory=lambda: str(Path.cwd()))
    pi_shell: str = field(default_factory=lambda: configuration_default(__file__, "pi_shell"))
    pi_binary: str = field(default_factory=lambda: configuration_default(__file__, "pi_binary"))
    instance_skills: list[str] = field(
        default_factory=lambda: configuration_default(__file__, "instance_skills")
    )
    main_model: str = field(default_factory=lambda: configuration_default(__file__, "main_model"))
    main_reasoning_effort: str = field(
        default_factory=lambda: configuration_default(__file__, "main_reasoning_effort")
    )
    assistant_name: str = field(default_factory=lambda: configuration_default(__file__, "assistant_name"))
    wake_words: list[str] = field(default_factory=lambda: configuration_default(__file__, "wake_words"))
    idle_seconds: int = field(default_factory=lambda: configuration_default(__file__, "idle_seconds"))
    voice_follow_up_seconds: int = field(
        default_factory=lambda: configuration_default(__file__, "voice_follow_up_seconds")
    )
    quiet_hours: dict[str, str | bool] = field(
        default_factory=lambda: configuration_default(__file__, "quiet_hours")
    )
    web: dict = field(default_factory=dict)
    apple_home: dict = field(default_factory=dict)
    events: dict = field(default_factory=dict)
    decisions: dict = field(default_factory=dict)
    interaction: dict = field(default_factory=lambda: configuration_default(__file__, "interaction"))
    ai_usage: dict = field(default_factory=dict)
    model_api: dict = field(default_factory=dict)
    media: dict = field(default_factory=dict)
    search: dict = field(default_factory=dict)
    hid_buttons: dict = field(default_factory=lambda: configuration_default(__file__, "hid_buttons"))
    notifications: dict = field(default_factory=dict)
    tasks: dict = field(default_factory=dict)
    execution: dict = field(default_factory=dict)
    work_files: dict = field(default_factory=lambda: configuration_default(__file__, "work_files"))
    input_device: str = field(default_factory=lambda: configuration_default(__file__, "input_device"))
    default_output: str = field(default_factory=lambda: configuration_default(__file__, "default_output"))
    minimum_volume: int = field(default_factory=lambda: configuration_default(__file__, "minimum_volume"))
    echo_cancel: bool = field(default_factory=lambda: configuration_default(__file__, "echo_cancel"))
    devices: dict[str, dict] = field(default_factory=lambda: configuration_default(__file__, "devices"))
    model_dir: str = field(default_factory=lambda: configuration_default(__file__, "model_dir"))
    unit_name: str = field(default_factory=lambda: configuration_default(__file__, "unit_name"))
    plugins: list[str] = field(default_factory=lambda: configuration_default(__file__, "plugins"))
    bindings: dict[str, str] = field(default_factory=lambda: configuration_default(__file__, "bindings"))
    plugin_settings: dict[str, dict] = field(default_factory=dict)
    external_mcp: dict = field(default_factory=dict)
    memory_enabled: bool = field(default_factory=lambda: configuration_default(__file__, "memory_enabled"))
    memory_auto_capture: bool = field(
        default_factory=lambda: configuration_default(__file__, "memory_auto_capture")
    )

    def __post_init__(self):
        if (
            not isinstance(self.plugins, list)
            or len(self.plugins) > 64
            or any(
                not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", name)
                for name in self.plugins
            )
            or len(set(self.plugins)) != len(self.plugins)
        ):
            raise ValueError("plugins must contain at most 64 unique plugin identifiers")
        if not isinstance(self.plugin_settings, dict) or any(
            not isinstance(value, dict) for value in self.plugin_settings.values()
        ):
            raise ValueError("plugin_settings must contain configuration tables")

        from assistant_runtime.core.configuration.owners import validate_stored

        from .extensions import discover

        validate_stored(self, discover)

        from assistant_runtime.app.skills import validate_names

        validate_names(self.instance_skills)

        from assistant_runtime.core.models.settings import ModelSettings

        model_settings = ModelSettings.parse(self.model_api)
        if model_settings.enabled and "models" not in self.plugins:
            raise ValueError("Enabled model API requires the models plugin")
        if self.interaction != {}:
            from assistant_runtime.core.speech.interaction.settings import InteractionSettings

            if InteractionSettings.parse(self.interaction).enabled:
                raise ValueError(
                    "Voice interaction is pending independent acceptance; disable interaction.enabled"
                )
        from assistant_runtime.core.plugin_catalog import OWNERS, declaration

        for name in self.plugins:
            if name not in OWNERS:
                continue
            spec = declaration(name)
            from assistant_runtime.api.system import system

            if system.name not in spec.supported_platforms:
                raise ValueError(f"Plugin {name} does not support {system.name}")
            if spec.validate and spec.config_key:
                spec.validate(getattr(self, spec.config_key))
        if "search" in self.plugins and not self.search.get("providers"):
            raise ValueError("Search plugin requires a configured provider")
        if self.quiet_hours != {}:
            from assistant_runtime.core.speech.quiet import QuietHours

            QuietHours.from_settings(self.quiet_hours)
        for name in (
            "main_model",
            "main_reasoning_effort",
            "realtime_voice",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a nonempty string")
            setattr(self, name, value.strip())
        if type(self.voice_follow_up_seconds) is not int or not 1 <= self.voice_follow_up_seconds <= 30:
            raise ValueError("voice_follow_up_seconds must be 1..30")

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
    def enabled_plugins(self):
        return tuple(name for name in self.plugins if name != "memory" or self.memory_enabled)

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
        p = p.resolve()
        data = read_config(p) if configured or p.exists() else {}
        unknown = set(data) - cls.__dataclass_fields__.keys()
        if unknown:
            raise ValueError(f"Unknown config keys: {sorted(unknown)}")
        if not p.exists() and not configured:
            data["profile"] = "headless"
        # Moving configuration must not move instance state; relative root is entry-relative.
        root = Path(data.pop("root", p.parent if p.exists() else default_root())).expanduser()
        root = (root if root.is_absolute() else p.parent / root).resolve()
        c = cls(root=root, **data)
        if c.profile not in {"voice", "headless"} or c.backend != "pi":
            raise ValueError("Unsupported profile or backend")
        if not 0 <= c.minimum_volume <= 100 or c.idle_seconds < 1:
            raise ValueError("Invalid volume, wake word or idle timeout")
        if c.profile == "voice":
            for ident, kind in ((c.input_device, "microphone"), (c.default_output, "speaker")):
                spec = c.devices.get(ident, {})
                if spec.get("kind") != kind or not spec.get("enabled", True):
                    raise ValueError(f"Configure an enabled {kind} device: {ident}")
        return c
