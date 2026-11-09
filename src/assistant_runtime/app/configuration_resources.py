"""Scoped configuration views shared by bundled and installed extensions."""

from pathlib import Path
from assistant_runtime.api.configuration_io import configuration_path


def resource_path(root, owner, declarations, name):
    resource = next((r for r in declarations if r["id"] == name), None)
    if resource is None:
        raise ValueError("Undeclared configuration resource")
    base = Path(root) / "config/plugins" / owner
    relative = Path(resource["path"])
    path = base / relative
    if (
        relative.is_absolute()
        or not relative.parts
        or not path.resolve().is_relative_to(base.resolve())
        or any(p.is_symlink() for p in (path, *path.parents))
    ):
        raise ValueError("Invalid configuration resource path")
    return configuration_path(path)


def settings(config, owner, spec):
    if spec.configuration and spec.configuration.namespace == "plugin_settings." + owner:
        return config.plugin_settings.get(owner, {})
    return getattr(config, spec.config_key) if spec.config_key else {}
