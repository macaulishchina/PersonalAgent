"""Serializable operator configuration descriptions; independent of plugin runtime."""

from dataclasses import dataclass, field
from typing import Any, Mapping
from functools import lru_cache
from copy import deepcopy
import json
from pathlib import Path
from typing import Callable, Awaitable


async def _nothing():
    pass


@dataclass
class ConfigurationUpdate:
    """Prepared owner change. Preparation never changes the active configuration.

    Rollback must also handle a partially completed commit. Release is called with
    whether the transaction committed, so owners dispose only superseded resources.
    """

    commit: Callable[[], Awaitable[None]] = _nothing
    rollback: Callable[[], Awaitable[None]] = _nothing
    release: Callable[[bool], Awaitable[None]] | None = None


class ConfigurationPending(Exception):
    """The owner must finish accepted work before the candidate can be prepared."""


@dataclass(frozen=True)
class ConfigurationResource:
    """Static private resource descriptor; it grants no unrestricted filesystem access."""

    id: str
    path: Path
    kind: str
    title: str
    sensitive: bool = False
    read_only: bool = False
    schema: dict | None = None


def scoped_update(update, context, value):
    """Publish only this owner's resolved settings after its commit succeeds."""
    if not isinstance(update, ConfigurationUpdate):
        raise TypeError("Owner must return ConfigurationUpdate")
    previous = context.config

    async def commit():
        await update.commit()
        context.config = value

    async def rollback():
        await update.rollback()
        context.config = previous

    return ConfigurationUpdate(commit, rollback, update.release)


@dataclass(frozen=True)
class ConfigurationPage:
    """Static UI mapping. Widget names refer to host-owned components, never scripts."""

    id: str
    title: str
    fields: tuple[Mapping[str, Any], ...] = ()
    actions: tuple[Mapping[str, Any], ...] = ()


class ConfigurationError(ValueError):
    """Safe field diagnostics; never include supplied values or provider exceptions."""

    def __init__(self, path, code, message):
        self.path, self.code, self.message = list(path), code, message
        super().__init__(f"{'.'.join(map(str, path)) or 'configuration'}: {message}")

    def public(self):
        return {"path": self.path, "code": self.code, "message": self.message}


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
    interface: Mapping[str, Any] = field(default_factory=dict)
    application: Mapping[str, str] = field(default_factory=dict)
    requires: tuple[str, ...] = ()


def configuration_default(owner_file, key=None):
    """A declared default is shared by native settings and the operator interface."""
    schema = _description(owner_file)["schema"]
    return deepcopy(schema["default"] if key is None else schema["properties"][key]["default"])


@lru_cache(maxsize=64)
def _description(owner_file):
    return json.loads(Path(owner_file).with_name("configuration.json").read_text(encoding="utf-8"))


def configuration_spec(owner_file):
    """Load the owner's shipped declaration without importing its runtime."""
    return ConfigurationSpec(**deepcopy(_description(owner_file)))
