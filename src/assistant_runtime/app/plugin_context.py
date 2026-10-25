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
        self.__registered = {}

    def register(self, owner, device_id, capabilities, probe):
        if owner != self.__owner:
            raise ValueError("Device registrar owner mismatch")
        result = self.__directory.register(owner, device_id, capabilities, probe)
        self.__registered[device_id] = probe
        return result

    async def close(self):
        for ident, probe in self.__registered.items():
            if self.__directory.probes.get(ident) is probe:
                for values in (
                    self.__directory.entries,
                    self.__directory.probes,
                    self.__directory.observations,
                    self.__directory.reading_clocks,
                ):
                    values.pop(ident, None)
        self.__registered.clear()

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

    def register(kind, handler, *, timeout=15):
        if kind not in provides:
            raise ValueError("Undeclared capability provider")
        box[0].register(kind, handler, timeout=timeout)

    public = PluginContext(
        name=name,
        config=settings,
        state_dir=state_dir,
        get=lambda key, default=None: store.get(prefix + key, default),
        set=lambda key, value: store.set(prefix + key, value),
        credential=credential,
        invoke=lambda request, timeout=None: runtime().invoke(request, timeout=timeout),
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
        for resource in (resources or {}).values():
            if isinstance(resource, (DeviceRegistration, ResourceRegistration)):
                context.own(resource.close)

    return public, attach


class BoundExtension:
    def __init__(self, plugin, public, attach):
        self.instance, self.public, self.attach = plugin, public, attach
        self.manifest = plugin.manifest

    def __getattr__(self, name):
        if name == "prepare_configuration":
            callback = getattr(self.instance, name)

            async def prepare(config):
                from assistant_runtime.core.plugin_catalog import declaration
                from assistant_runtime.core.configuration.schema import resolve_definition
                from assistant_runtime.api.configuration import scoped_update

                spec = declaration(self.manifest.name)
                value = resolve_definition(spec, getattr(config, spec.config_key))
                return scoped_update(await callback(value), self.public, value)

            return prepare
        return getattr(self.instance, name)

    async def start(self, context):
        self.attach(context)
        await self.instance.start(self.public)


class ResourceRegistration:
    """Owner-bound device provider registration; no provider inventory is exposed."""

    def __init__(self, registry, owner, readable=()):
        self.__registry, self.__owner, self.__readable = registry, owner, frozenset(readable)
        self.__registered = {}

    def register(self, owner, device_id, provider, **kwargs):
        if owner != self.__owner:
            raise ValueError("Resource registrar owner mismatch")
        result = self.__registry.register(owner, device_id, provider, **kwargs)
        self.__registered[device_id] = provider
        return result

    async def close(self):
        for ident, provider in self.__registered.items():
            if self.__registry.providers.get(ident) is provider:
                self.__registry.providers.pop(ident)
                getattr(self.__registry, "sharing", {}).pop(ident, None)
        self.__registered.clear()

    def get(self, device_id):
        if device_id not in self.__readable:
            raise ValueError("Resource access was not declared")
        return self.__registry.get(device_id)

    async def open(self, device_id, *args, **kwargs):
        if device_id not in self.__readable:
            raise ValueError("Resource access was not declared")
        return await self.__registry.open(device_id, *args, **kwargs)
