"""Declarative implementation selection; never imports a backend or probes hardware."""

from dataclasses import dataclass, field
import json
from pathlib import Path
import re
from types import MappingProxyType
from typing import Mapping

from assistant_runtime.kernel.platform import PlatformInfo as PlatformInfo


@dataclass(frozen=True)
class BackendSpec:
    implementations: Mapping[str, tuple[str, ...]]
    defaults: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self):
        choices = {}
        if not isinstance(self.implementations, Mapping) or not self.implementations:
            raise ValueError("Backend implementations must declare platforms")
        for platform, names in self.implementations.items():
            if (
                platform not in {"linux", "windows"}
                or not isinstance(names, (list, tuple))
                or not 1 <= len(names) <= 16
            ):
                raise ValueError("Invalid platform backend list")
            if any(
                not isinstance(n, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", n) for n in names
            ) or len(set(names)) != len(names):
                raise ValueError("Invalid or duplicate backend identifier")
            choices[platform] = tuple(names)
        if not isinstance(self.defaults, Mapping) or any(
            p not in choices or value not in choices[p] for p, value in self.defaults.items()
        ):
            raise ValueError("Default backend must be declared on its platform")
        object.__setattr__(self, "implementations", MappingProxyType(choices))
        object.__setattr__(self, "defaults", MappingProxyType(dict(self.defaults)))

    def select(self, platform, configured=None):
        names = self.implementations.get(platform, ())
        if not names:
            raise ValueError(f"No implementation for platform {platform}")
        selected = (configured or {}).get(platform, self.defaults.get(platform))
        if selected is None and len(names) == 1:
            return names[0]
        if selected is None:
            raise ValueError(f"Multiple implementations on {platform}; configure a default implementation")
        if selected not in names:
            raise ValueError(
                f"Implementation {selected!r} is unavailable on {platform}; available: {', '.join(names)}"
            )
        return selected

    def describe(self):
        return {
            "implementations": {p: list(v) for p, v in self.implementations.items()},
            "defaults": dict(self.defaults),
        }


def plugin_backends(directory):
    path = Path(directory)
    path = path if path.suffix == ".json" else path / "platform.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Backend metadata must be an object")
    if "backends" not in data:
        if "default_backends" in data:
            raise ValueError("Backend defaults require implementation declarations")
        return None
    return BackendSpec(data["backends"], data.get("default_backends", {}))


def select_backend(name, spec, platform, configured):
    if spec is None:
        if configured.get(platform):
            raise ValueError(f"Plugin {name} does not declare selectable implementations")
        return None
    try:
        return spec.select(platform, configured)
    except ValueError as exc:
        raise ValueError(f"Plugin {name}: {exc}") from exc
