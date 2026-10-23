"""Public extension contracts. No host or provider implementation is exposed here."""

from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping
import asyncio
import logging
import time

from assistant_runtime.kernel.runtime import Manifest


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
    read_only: bool
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
    ):
        self.name, self.config = name, config
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

    @property
    def identity(self):
        return current_call.get()

    def resource(self, name):
        if name not in self.__resources:
            raise ValueError("Undeclared plugin resource: " + name)
        return self.__resources[name]

    def repository(self, factory, *args, **kwargs):
        if self.__repository is None:
            raise RuntimeError("Plugin repository unavailable")
        return self.__repository(factory, *args, **kwargs)

    def register(self, request_type, handler):
        if self.__register is None:
            raise RuntimeError("Plugin cannot register this capability")
        self.__register(request_type, handler)

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

    async def invoke(self, request, timeout=15):
        if type(request) not in self.__capabilities:
            raise ValueError("Capability not declared by this plugin")
        if self.cancelled.is_set():
            raise RuntimeError("Plugin stopped")
        identity = self.identity
        if identity and identity.deadline is not None:
            timeout = min(timeout, identity.deadline - time.monotonic())
        if timeout <= 0:
            raise TimeoutError("Request deadline exceeded")
        return await self.__invoke(request, timeout=timeout)

    def publish(self, kind, **data):
        self.__publish(kind, **data)

    def subscribe(self, capacity=128):
        return self.__subscribe(capacity)

    def spawn(self, coroutine, label="worker"):
        return self.__spawn(coroutine, label)

    def own(self, cleanup):
        self.__own(cleanup)
