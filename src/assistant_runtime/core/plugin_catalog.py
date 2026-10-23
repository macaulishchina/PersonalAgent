"""Bundled owner identities only. Each owner declares its settings, skills and tools."""

from importlib import import_module

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
    return import_module(OWNERS[name] + ("_declaration" if name == "gesture" else ".declaration")).DECLARATION


def services(enabled=None):
    names = OWNERS if enabled is None else (name for name in enabled if name in OWNERS)
    return {spec.service: spec for name in names if (spec := declaration(name)).service}
