"""Instance configuration. Hardware identities never belong in code defaults."""

from assistant_runtime.api.configuration import configuration_default
from assistant_runtime.core.plugin_catalog import metadata
from .presets import default_plugins
from .configuration_fields import ModuleSettings

from dataclasses import dataclass, field
from pathlib import Path
import os
import re
import tomllib


ROOT = Path(__file__).resolve().parents[2]


def default_root():
    return Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") / "personal-agent"


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
class Config(ModuleSettings):
    root: Path = field(default_factory=default_root)
    profile: str = field(default_factory=lambda: configuration_default(__file__, "profile"))
    backend: str = field(default_factory=lambda: configuration_default(__file__, "backend"))
    pi_shell: str = field(default_factory=lambda: configuration_default(__file__, "pi_shell"))
    pi_binary: str = field(default_factory=lambda: configuration_default(__file__, "pi_binary"))
    instance_skills: list[str] = field(
        default_factory=lambda: configuration_default(__file__, "instance_skills")
    )
    main_model: str = field(default_factory=lambda: configuration_default(__file__, "main_model"))
    main_reasoning_effort: str = field(
        default_factory=lambda: configuration_default(__file__, "main_reasoning_effort")
    )
    web: dict = field(default_factory=dict)
    apple_home: dict = field(default_factory=dict)
    events: dict = field(default_factory=dict)
    decisions: dict = field(default_factory=dict)
    ai_usage: dict = field(default_factory=dict)
    model_api: dict = field(default_factory=dict)
    media: dict = field(default_factory=dict)
    search: dict = field(default_factory=dict)
    hid_buttons: dict = field(default_factory=lambda: configuration_default(metadata("hid_buttons")))
    notifications: dict = field(default_factory=dict)
    tasks: dict = field(default_factory=dict)
    execution: dict = field(default_factory=dict)
    model_dir: str = field(default_factory=lambda: configuration_default(__file__, "model_dir"))
    unit_name: str = field(default_factory=lambda: configuration_default(__file__, "unit_name"))
    plugins: list[str] = field(default_factory=default_plugins)
    bindings: dict[str, str] = field(default_factory=lambda: configuration_default(__file__, "bindings"))
    plugin_settings: dict[str, dict] = field(default_factory=dict)
    plugin_backends: dict[str, dict[str, str]] = field(default_factory=dict)
    external_mcp: dict = field(default_factory=dict)

    def __post_init__(self):
        self.root = Path(self.root).expanduser().resolve()
        workspace = Path(self.workspace).expanduser() if self.workspace else self.root
        self.workspace = str((self.root / workspace).resolve())
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

        if not isinstance(self.plugin_backends, dict) or len(self.plugin_backends) > 64:
            raise ValueError("plugin_backends must contain at most 64 plugin defaults")
        for owner, values in self.plugin_backends.items():
            if (
                not isinstance(owner, str)
                or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", owner)
                or not isinstance(values, dict)
            ):
                raise ValueError("Invalid plugin backend defaults")
            if any(
                platform not in {"linux", "windows"}
                or not isinstance(value, str)
                or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", value)
                for platform, value in values.items()
            ):
                raise ValueError(
                    "Backend defaults require linux/windows and valid implementation identifiers"
                )

        from .plugin_selection import resolve_selection
        from assistant_runtime.api.system import system

        resolve_selection(self.enabled_plugins, self.plugin_backends, system.name)

        from assistant_runtime.core.configuration.owners import validate_stored

        from .extensions import discover

        validate_stored(self, discover)
        for section, key in (
            (self.work_files, "root"),
            (self.external_mcp, "registry_path"),
            (self.web, "token_file"),
        ):
            if section.get(key):
                section[key] = str((self.root / Path(section[key]).expanduser()).resolve())

        from assistant_runtime.app.skills import validate_names

        validate_names(self.instance_skills)

        from assistant_runtime.core.models.settings import ModelSettings

        if "models" in self.enabled_plugins:
            model_settings = ModelSettings.parse(self.model_api)
            if self.tasks:
                from assistant_runtime.core.tasks.settings import TaskSettings

                TaskSettings.parse(self.tasks).validate_bindings(model_settings.entries)
        if "assistant" in self.enabled_plugins and self.interaction != {}:
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
            if spec.validate and spec.config_key:
                spec.validate(getattr(self, spec.config_key))
        if "search" in self.plugins and not self.search.get("providers"):
            raise ValueError("Search plugin requires a configured provider")
        if "assistant" in self.enabled_plugins and self.quiet_hours != {}:
            from assistant_runtime.core.speech.quiet import QuietHours

            QuietHours.from_settings(self.quiet_hours)
        for name in (
            "main_model",
            "main_reasoning_effort",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a nonempty string")
            setattr(self, name, value.strip())
        if "assistant" in self.enabled_plugins and (
            type(self.voice_follow_up_seconds) is not int or not 1 <= self.voice_follow_up_seconds <= 30
        ):
            raise ValueError("voice_follow_up_seconds must be 1..30")

        if "devices" in self.enabled_plugins:
            from assistant_runtime.core.devices.configuration_validation import validate_devices

            validate_devices(self)
        if "assistant" in self.enabled_plugins and (
            not isinstance(self.wake_words, list)
            or not self.wake_words
            or any(not isinstance(word, str) or not word.strip() for word in self.wake_words)
        ):
            raise ValueError("wake_words must be a nonempty list of nonempty strings")
        if not isinstance(self.wake_words, list) or any(
            not isinstance(word, str) for word in self.wake_words
        ):
            raise ValueError("wake_words must be a list of strings")
        self.wake_words = [word.strip() for word in self.wake_words]
        if "assistant" in self.enabled_plugins and len(set(self.wake_words)) != len(self.wake_words):
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
        return (
            (self.root / Path(self.model_dir).expanduser()).resolve()
            if self.model_dir
            else self.root / "models"
        )

    @property
    def socket(self):
        return self.state_dir / "control.sock"

    @classmethod
    def load(cls, path=None):
        configured = path or os.environ.get("PERSONAL_AGENT_CONFIG")
        p = (
            Path(configured).expanduser()
            if configured
            else Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
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
        root = data.pop("root", None)
        root = Path(root if root is not None else p.parent if p.exists() else default_root()).expanduser()
        root = (root if root.is_absolute() else p.parent / root).resolve()
        c = cls(root=root, **data)
        if c.profile not in {"voice", "headless"} or c.backend != "pi":
            raise ValueError("Unsupported profile or backend")
        if c.profile == "voice":
            for ident, kind in ((c.input_device, "microphone"), (c.default_output, "speaker")):
                spec = c.devices.get(ident, {})
                if spec.get("kind") != kind or not spec.get("enabled", True):
                    raise ValueError(f"Configure an enabled {kind} device: {ident}")
        return c
