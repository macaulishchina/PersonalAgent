"""Bundled owner identities only. Each owner declares its settings, skills and tools."""

from importlib import import_module
from pathlib import Path
from assistant_runtime.api.plugins import plugin_platforms
from assistant_runtime.api.backends import plugin_backends

import json

OWNERS = json.loads((Path(__file__).resolve().parents[1] / "owners.json").read_text(encoding="utf-8"))


def declaration(name):
    spec = import_module(OWNERS[name] + ("_declaration" if name == "gesture" else ".declaration")).DECLARATION
    if set(spec.supported_platforms) != set(platforms(name)):
        raise ValueError(f"Plugin {name}: static and runtime platform certifications differ")
    if spec.backends != backends(name):
        raise ValueError(f"Plugin {name}: static and runtime backend declarations differ")
    from assistant_runtime.app.versions import manifest, CompatibilityError

    if spec.manifest != manifest(release(name)):
        raise CompatibilityError(f"Plugin {name}: static and runtime release declarations differ")
    return spec


def metadata(name):
    root = Path(__file__).resolve().parents[1]
    path = root.joinpath(*OWNERS[name].removeprefix("assistant_runtime.").split("."))
    return path.with_suffix(".plugin.json") if name == "gesture" else path / "plugin.json"


def release(name):
    from assistant_runtime.app.versions import read_descriptor

    return read_descriptor(metadata(name), core=OWNERS[name].startswith("assistant_runtime.core."))


def backends(name):
    module = OWNERS[name]
    root = Path(__file__).resolve().parents[1]
    path = root.joinpath(*module.removeprefix("assistant_runtime.").split("."))
    metadata = path.with_suffix(".plugin.json") if name == "gesture" else path / "plugin.json"
    return plugin_backends(metadata) if metadata.is_file() else None


def platforms(name):
    module = OWNERS[name]
    root = Path(__file__).resolve().parents[1]
    path = root.joinpath(*module.removeprefix("assistant_runtime.").split("."))
    metadata = path.with_suffix(".plugin.json") if name == "gesture" else path / "plugin.json"
    if module.startswith("assistant_runtime.core.") and not metadata.exists():
        return ("linux", "windows")
    return plugin_platforms(metadata)


def services(enabled=None):
    names = OWNERS if enabled is None else (name for name in enabled if name in OWNERS)
    return {spec.service: spec for name in names if (spec := declaration(name)).service}
