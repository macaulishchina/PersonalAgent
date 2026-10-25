"""Operator inventory of enabled, explicitly installed plugins. No secret values."""

from importlib.metadata import entry_points, version, PackageNotFoundError
import json
import os
import tempfile
from assistant_runtime.core.plugin_catalog import OWNERS, declaration
from assistant_runtime.api.system import system
from .plugin_selection import resolve_selection
from .extensions import discover


def inventory(config):
    selected = resolve_selection(config.enabled_plugins, config.plugin_backends, system.name)
    external = [name for name in config.enabled_plugins if name not in OWNERS]
    definitions = discover(external, OWNERS) if external else {}
    sources = {
        entry.name: entry.dist
        for entry in entry_points(group="personal_agent.plugins")
        if entry.name in external and getattr(entry, "dist", None) is not None
    }
    records = []
    for name in config.enabled_plugins:
        spec = declaration(name) if name in OWNERS else definitions[name]
        distribution = sources.get(name)
        records.append(
            {
                "name": name,
                "kind": "core"
                if name in OWNERS and OWNERS[name].startswith("assistant_runtime.core.")
                else "extension",
                "version": spec.manifest.version,
                "api_version": spec.manifest.api_version,
                "requires": list(spec.manifest.requires),
                "optional": list(spec.manifest.optional),
                "supported_platforms": list(spec.supported_platforms),
                "backend": selected[name],
                "platform_supported": system.name in spec.supported_platforms,
                "distribution": {"name": distribution.metadata["Name"], "version": distribution.version}
                if distribution
                else None,
            }
        )
    try:
        application_version = version("personal-agent")
    except PackageNotFoundError:
        application_version = "source"
    return {"application_version": application_version, "platform": system.name, "plugins": records}


def persist(config):
    data = inventory(config)
    config.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=config.state_dir, prefix=".plugins-")
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(data, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, config.state_dir / "plugin-installation.json")
    finally:
        if os.path.exists(name):
            os.unlink(name)
    return data
