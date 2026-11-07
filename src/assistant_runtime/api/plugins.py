"""Public extension contracts. No host or provider implementation is exposed here."""

from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping
import asyncio
import logging
import time

from assistant_runtime.kernel.runtime import Manifest
from .configuration import ConfigurationSpec
from .backends import BackendSpec, PlatformInfo


def plugin_platforms(directory):
    """Static owner metadata, usable without importing a plugin or its dependencies."""
    import json

    path = Path(directory)
    path = path if path.suffix == ".json" else path / "plugin.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        values = data["supported_platforms"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ValueError("Missing or invalid static plugin platform certification") from exc
    if (
        not isinstance(values, list)
        or not values
        or any(not isinstance(value, str) or value not in {"linux", "windows"} for value in values)
        or len(values) != len(set(values))
    ):
        raise ValueError("Invalid static plugin platform declaration")
    return tuple(values)


class PlatformCertificationError(ValueError):
    """Safe operator-facing platform refusal; contains no instance configuration."""


def require_platform(name, platforms, current):
    if current not in platforms:
        certified = ", ".join(platforms) or "none"
        raise PlatformCertificationError(
            f"Plugin {name} does not support {current}; certified platforms: {certified}"
        )


@dataclass(frozen=True)
class CallIdentity:
    request_id: str
    work_id: str
    task_run: str | None = None
    deadline: float | None = None


current_call: ContextVar[CallIdentity | None] = ContextVar("plugin_call", default=None)


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_schema: Mapping[str, Any]
    output_schema: Mapping[str, Any]
    handler: Callable[[dict], Awaitable[Any]]
    read_only: bool = False
    destructive: bool = False
    idempotent: bool = False
    external: bool = False

    @property
    def annotations(self):
        return {
            "readOnlyHint": self.read_only,
            "destructiveHint": self.destructive,
            "idempotentHint": self.idempotent,
            "openWorldHint": self.external,
        }


@dataclass(frozen=True)
class PluginDefinition:
    manifest: Manifest
    create: Callable[["PluginContext"], Any] | None = None
    validate: Callable[[dict], Any] | None = None
    skills: tuple[Path, ...] = ()
    uses: tuple[type, ...] = ()
    credentials: tuple[str, ...] = ()
    provides: tuple[type, ...] = ()
    resources: tuple[str, ...] = ()
    internal_factory: Callable | None = None
    config_fields: tuple[str, ...] = ()
    config_key: str | None = None
    service: str | None = None
    tools_factory: str | None = None
    task_context: bool = True
    background_tools: tuple[str, ...] | None = None
    configuration: ConfigurationSpec | None = None
    supported_platforms: tuple[str, ...] = ("linux", "windows")
    backends: BackendSpec | None = None
    operation_request: type | None = None

    def __post_init__(self):
        if self.operation_request is not None and self.operation_request not in self.provides:
            raise ValueError("Operation observations require a declared public capability")
        if not self.supported_platforms or set(self.supported_platforms) - {"linux", "windows"}:
            raise ValueError("Plugin must declare supported platforms: linux and/or windows")
        if self.backends is not None and not isinstance(self.backends, BackendSpec):
            raise ValueError("Plugin backend declaration must be a BackendSpec")


class PluginContext:
    """Explicitly injected handles, not a registry of concrete plugin instances.

    This API boundary is for trusted installed code, not a Python sandbox.
    State and credential callbacks are bound to this plugin by the application.
    """

    def __init__(
        self,
        *,
        name,
        config,
        state_dir,
        get,
        set,
        credential,
        invoke,
        publish,
        subscribe,
        spawn,
        own,
        cancelled,
        capabilities=(),
        repository=None,
        resources=None,
        register=None,
        reporter=None,
        readiness=None,
        health=None,
        dropped=None,
        configuration_resource=None,
        platform=None,
        backend=None,
        available=None,
    ):
        self.name, self.config = name, config
        self.__platform = platform or PlatformInfo.current()
        self.__backend = backend
        self.__available = available or (lambda request: False)
        self.state_dir = Path(state_dir)
        self.log = logging.getLogger("plugin." + name)
        self.cancelled: asyncio.Event = cancelled
        self.__get, self.__set, self.__credential = get, set, credential
        self.__invoke, self.__publish, self.__subscribe = invoke, publish, subscribe
        self.__spawn, self.__own = spawn, own
        self.__capabilities = frozenset(capabilities)
        self.__repository = repository
        self.__resources = resources or {}
        self.__register, self.__reporter = register, reporter
        self.ready = readiness or asyncio.Event()
        self.__health = health
        self.__dropped = dropped or (lambda: 0)
        self.__configuration_resource = configuration_resource

    @property
    def platform(self):
        return self.__platform

    @property
    def backend(self):
        return self.__backend

    @property
    def identity(self):
        return current_call.get()

    def resource(self, name):
        if name not in self.__resources:
            raise ValueError("Undeclared plugin resource: " + name)
        return self.__resources[name]

    def configuration_resource(self, name):
        """Read a declared private configuration resource from the loaded generation."""
        if self.__configuration_resource is None:
            raise ValueError("No configuration resource adapter")
        return self.__configuration_resource(name)

    def repository(self, factory, *args, **kwargs):
        if self.__repository is None:
            raise RuntimeError("Plugin repository unavailable")
        return self.__repository(factory, *args, **kwargs)

    def register(self, request_type, handler, *, timeout=15):
        if self.__register is None:
            raise RuntimeError("Plugin cannot register this capability")
        self.__register(request_type, handler, timeout=timeout)

    def event_reporter(self, source=None):
        if self.__reporter is None:
            raise RuntimeError("Event reporting unavailable")
        return self.__reporter(source)

    def set_health(self, state, error=None):
        if state not in {"ready", "degraded"} or self.__health is None:
            raise ValueError("Invalid plugin health transition")
        self.__health(state, error)

    @property
    def dropped(self):
        return self.__dropped()

    def get(self, key, default=None):
        return self.__get(key, default)

    def set(self, key, value):
        self.__set(key, value)

    def credential(self, name):
        return self.__credential(name)

    async def invoke(self, request, timeout=None):
        if type(request) not in self.__capabilities:
            raise ValueError("Capability not declared by this plugin")
        if self.cancelled.is_set():
            raise RuntimeError("Plugin stopped")
        identity = self.identity
        if identity and identity.deadline is not None:
            timeout = (
                (identity.deadline - time.monotonic())
                if timeout is None
                else min(timeout, identity.deadline - time.monotonic())
            )
        if timeout is not None and timeout <= 0:
            raise TimeoutError("Request deadline exceeded")
        return await self.__invoke(request, timeout=timeout)

    def available(self, request_type):
        """Read readiness only for a capability already declared by this plugin."""
        if request_type not in self.__capabilities:
            raise ValueError("Capability not declared by this plugin")
        return not self.cancelled.is_set() and self.__available(request_type)

    def publish(self, kind, **data):
        self.__publish(kind, **data)

    def subscribe(self, capacity=128):
        return self.__subscribe(capacity)

    def spawn(self, coroutine, label="worker"):
        return self.__spawn(coroutine, label)

    def own(self, cleanup):
        self.__own(cleanup)
