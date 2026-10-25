"""One static activation check for every bundled and installed plugin."""

from assistant_runtime.api.backends import select_backend
from assistant_runtime.api.plugins import PlatformCertificationError, require_platform
from assistant_runtime.core import plugin_catalog
from .plugin_metadata import installed_platforms, installed_backends


def resolve_selection(names, defaults, platform):
    from .extensions import installed_entries

    external = [name for name in names if name not in plugin_catalog.OWNERS]
    entries = installed_entries(external, plugin_catalog.OWNERS) if external else {}
    selected = {}
    for name in names:
        try:
            certified = (
                plugin_catalog.platforms(name)
                if name in plugin_catalog.OWNERS
                else installed_platforms(entries[name])
            )
        except ValueError as exc:
            raise PlatformCertificationError(
                f"Plugin {name} cannot enable on {platform}; certified platforms: none "
                "(missing or invalid static platform certification)"
            ) from exc
        require_platform(name, certified, platform)
        policy = (
            plugin_catalog.backends(name)
            if name in plugin_catalog.OWNERS
            else installed_backends(entries[name])
        )
        selected[name] = select_backend(name, policy, platform, defaults.get(name, {}))
    return selected
