"""Bundled owner identities only. Each owner declares its settings, skills and tools."""

from importlib import import_module
from pathlib import Path
from assistant_runtime.api.plugins import plugin_platforms
from assistant_runtime.api.backends import plugin_backends

OWNERS = {
    **{
        name: "assistant_runtime.core." + name
        for name in (
            "ai_usage",
            "models",
            "pi",
            "execution",
            "workfiles",
            "conversation",
            "cli",
            "memory",
            "tasks",
            "notifications",
            "events",
            "integrations",
        )
    },
    "assistant": "assistant_runtime.core.speech",
    "devices": "assistant_runtime.core.devices",
    **{
        name: "assistant_runtime.plugins." + name
        for name in (
            "pipewire",
            "airplay",
            "apple_home",
            "go2rtc",
            "web",
            "media",
            "search",
            "decisions",
            "observations",
            "hid_buttons",
            "keyword",
            "v4l2",
        )
    },
    "voice_disabled": "assistant_runtime.plugins.speech",
    "gesture": "assistant_runtime.plugins.gesture",
}


def declaration(name):
    spec = import_module(OWNERS[name] + ("_declaration" if name == "gesture" else ".declaration")).DECLARATION
    if set(spec.supported_platforms) != set(platforms(name)):
        raise ValueError(f"Plugin {name}: static and runtime platform certifications differ")
    if spec.backends != backends(name):
        raise ValueError(f"Plugin {name}: static and runtime backend declarations differ")
    return spec


def backends(name):
    module = OWNERS[name]
    root = Path(__file__).resolve().parents[1]
    path = root.joinpath(*module.removeprefix("assistant_runtime.").split("."))
    metadata = path.with_suffix(".platform.json") if name == "gesture" else path / "platform.json"
    return plugin_backends(metadata) if metadata.is_file() else None


def platforms(name):
    module = OWNERS[name]
    root = Path(__file__).resolve().parents[1]
    path = root.joinpath(*module.removeprefix("assistant_runtime.").split("."))
    metadata = path.with_suffix(".platform.json") if name == "gesture" else path / "platform.json"
    if module.startswith("assistant_runtime.core.") and not metadata.exists():
        return ("linux", "windows")
    return plugin_platforms(metadata)


def services(enabled=None):
    names = OWNERS if enabled is None else (name for name in enabled if name in OWNERS)
    return {spec.service: spec for name in names if (spec := declaration(name)).service}
