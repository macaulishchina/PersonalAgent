"""Application-only binding of restricted public handles for bundled extensions."""

import asyncio
from pathlib import Path
from assistant_runtime.api import PluginContext


class ScopedState:
    def __init__(self, get, set, *, keys=(), prefixes=()):
        self.__get, self.__set = get, set
        self.__keys, self.__prefixes = keys, prefixes

    def check(self, key):
        if key not in self.__keys and not any(key.startswith(prefix) for prefix in self.__prefixes):
            raise ValueError("Undeclared plugin state")

    def get(self, key, default=None):
        self.check(key)
        return self.__get(key, default)

    def set(self, key, value):
        self.check(key)
        return self.__set(key, value)


class UsageAccess:
    """Admission protocol only; does not expose the usage ledger or configuration."""

    def __init__(self, scope, admit, settle):
        self.scope, self.admit, self.settle = scope, admit, settle


class DeviceRegistration:
    def __init__(self, directory, owner):
        self.__directory, self.__owner = directory, owner

    def register(self, owner, device_id, capabilities, probe):
        if owner != self.__owner:
            raise ValueError("Device registrar owner mismatch")
        return self.__directory.register(owner, device_id, capabilities, probe)

    def observe(self, owner, device_id, observation):
        if owner != self.__owner:
            raise ValueError("Device observation owner mismatch")
        return self.__directory.observe(owner, device_id, observation)


def environment(
    host, name, settings, *, resources=None, uses=(), provides=(), credentials=(), state_name=None
):
    box = []

    def runtime():
        return host.runtime

    root = Path(host.config.root)
    store = getattr(host, "store", None) or host.runtime.store
    prefix = "plugin." + name + "."
    state_dir = getattr(host.config, "state_dir", root / "state") / (state_name or name)

    def repository(factory, *args, **kwargs):
        if not factory.__module__.startswith("assistant_runtime.plugins." + name + "."):
            raise ValueError("Repository must belong to its plugin")
        return factory(store.db, *args, **kwargs)

    def credential(key):
        from .extensions import extension_credential
        from types import SimpleNamespace

        return extension_credential(root, SimpleNamespace(credentials=credentials), key)

    def subscribe(capacity):
        return box[0].subscribe(capacity)

    def register(kind, handler):
        if kind not in provides:
            raise ValueError("Undeclared capability provider")
        box[0].register(kind, handler)

    public = PluginContext(
        name=name,
        config=settings,
        state_dir=state_dir,
        get=lambda key, default=None: store.get(prefix + key, default),
        set=lambda key, value: store.set(prefix + key, value),
        credential=credential,
        invoke=lambda request, timeout=15: runtime().invoke(request, timeout=timeout),
        publish=lambda kind, **data: box[0].publish(kind, **data),
        subscribe=subscribe,
        spawn=lambda coro, label="worker": box[0].spawn(coro, label),
        own=lambda cleanup: box[0].own(cleanup),
        cancelled=asyncio.Event(),
        capabilities=uses,
        repository=repository,
        resources=resources,
        register=register,
        reporter=lambda source=None: box[0].event_reporter(source),
        readiness=asyncio.Event(),
        health=lambda state, error: box[0].set_health(state, error),
        dropped=lambda: box[0].dropped,
    )

    def attach(context):
        box[:] = [context]
        public.ready = context.ready
        public.cancelled = context.cancelled

    return public, attach


class BoundExtension:
    def __init__(self, plugin, public, attach):
        self.instance, self.public, self.attach = plugin, public, attach
        self.manifest = plugin.manifest

    def __getattr__(self, name):
        return getattr(self.instance, name)

    async def start(self, context):
        self.attach(context)
        await self.instance.start(self.public)


class ResourceRegistration:
    """Owner-bound device provider registration; no provider inventory is exposed."""

    def __init__(self, registry, owner, readable=()):
        self.__registry, self.__owner, self.__readable = registry, owner, frozenset(readable)

    def register(self, owner, device_id, provider, **kwargs):
        if owner != self.__owner:
            raise ValueError("Resource registrar owner mismatch")
        return self.__registry.register(owner, device_id, provider, **kwargs)

    def get(self, device_id):
        if device_id not in self.__readable:
            raise ValueError("Resource access was not declared")
        return self.__registry.get(device_id)

    async def open(self, device_id, *args, **kwargs):
        if device_id not in self.__readable:
            raise ValueError("Resource access was not declared")
        return await self.__registry.open(device_id, *args, **kwargs)
