"""Serializable operator configuration descriptions; independent of plugin runtime."""

from dataclasses import dataclass, field
from typing import Any, Mapping
from functools import lru_cache
from copy import deepcopy
import json
from pathlib import Path


@dataclass(frozen=True)
class ConfigurationSpec:
    """Schema describes stored values; the owner validator remains authoritative."""

    version: int = 1
    namespace: str = ""
    schema: Mapping[str, Any] = field(default_factory=lambda: {"type": "object"})
    title: str = ""
    group: str = "extensions"
    description: str = ""
    sensitive: tuple[str, ...] = ()
    read_only: tuple[str, ...] = ()
    resources: tuple[Mapping[str, Any], ...] = ()
    supported_platforms: tuple[str, ...] = ("linux", "windows")


def configuration_default(owner_file, key):
    """A declared default is shared by native settings and the operator interface."""
    return deepcopy(_description(owner_file)["schema"]["properties"][key]["default"])


@lru_cache(maxsize=64)
def _description(owner_file):
    return json.loads(Path(owner_file).with_name("configuration.json").read_text(encoding="utf-8"))


def configuration_spec(owner_file):
    """Load the owner's shipped declaration without importing its runtime."""
    return ConfigurationSpec(**deepcopy(_description(owner_file)))
