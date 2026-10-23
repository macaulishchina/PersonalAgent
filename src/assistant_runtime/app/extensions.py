"""Explicit installed-extension discovery and owned public-context construction."""

import asyncio
import copy
from importlib.metadata import entry_points
import re

from assistant_runtime.api import CallIdentity, PluginContext, PluginDefinition
from assistant_runtime.api.plugins import current_call

GROUP = "personal_agent.plugins"


def discover(names, builtins=()):
    """Read metadata without importing disabled extensions; reject ambiguous ownership."""
    if len(names) != len(set(names)):
        raise ValueError("Duplicate enabled plugin")
    found = {}
    for entry in entry_points(group=GROUP):
        if entry.name in builtins:
            raise ValueError("Installed extension shadows a bundled plugin: " + entry.name)
        if entry.name not in names:
            continue
        if entry.name in found:
            raise ValueError("Duplicate installed plugin: " + entry.name)
        found[entry.name] = entry
    result = {}
    for name in names:
        if name not in found:
            raise ValueError("Plugin not installed: " + name)
        definition = found[name].load()
        definition = definition() if callable(definition) else definition
        if not isinstance(definition, PluginDefinition) or definition.manifest.name != name:
            raise ValueError("Invalid plugin declaration: " + name)
        if definition.internal_factory is not None or definition.create is None:
            raise ValueError("Installed plugins require a public-context factory")
        if definition.resources:
            raise ValueError(
                "Installed plugin requested unsupported direct resources; use declared capabilities"
            )
        if definition.manifest.critical:
            raise ValueError("Extensions cannot declare themselves critical")
        result[name] = definition
    return result


class ToolRegistry:
    """One schema and handler shared by direct and MCP callers. Identity is host-bound."""

    def __init__(self):
        self.tools = {}
        self.guards = {}
        self.active = {}

    def register(self, owner, tool):
        from jsonschema import Draft202012Validator

        if not re.fullmatch(r"[a-z][a-z0-9_]{0,95}", tool.name) or not tool.description.strip():
            raise ValueError("Invalid tool declaration")
        if tool.name in self.tools:
            raise ValueError("Duplicate tool: " + tool.name)
        if tool.read_only and tool.destructive:
            raise ValueError("Read-only tools cannot be destructive")
        for schema in (tool.input_schema, tool.output_schema):
            Draft202012Validator.check_schema(schema)
        self.tools[tool.name] = owner, tool

    def describe(self):
        items = []
        for name, (owner, tool) in self.tools.items():
            try:
                if owner in self.guards:
                    self.guards[owner]()
            except RuntimeError:
                continue
            items.append(
                {
                    "name": name,
                    "description": tool.description,
                    "input_schema": dict(tool.input_schema),
                    "output_schema": dict(tool.output_schema),
                    "annotations": tool.annotations,
                }
            )
        return {"tools": items}

    def remove(self, owner):
        self.tools = {k: v for k, v in self.tools.items() if v[0] != owner}
        self.guards.pop(owner, None)

    async def close_owner(self, owner):
        pending = set(self.active.get(owner, ())) - {asyncio.current_task()}
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        self.remove(owner)

    async def invoke(self, name, arguments, *, identity: CallIdentity, timeout=30):
        from jsonschema import Draft202012Validator
        import time

        owner, tool = self.tools[name]
        guard = self.guards.get(owner)
        generation = guard() if guard else None
        Draft202012Validator(tool.input_schema).validate(arguments)
        if identity.deadline is not None:
            timeout = min(timeout, identity.deadline - time.monotonic())
        if timeout <= 0:
            raise TimeoutError("Request deadline exceeded")
        token = current_call.set(identity)
        current = asyncio.current_task()
        active = self.active.setdefault(owner, set())
        active.add(current)
        try:
            result = await asyncio.wait_for(tool.handler(copy.deepcopy(arguments)), timeout)
            if guard and guard() != generation:
                raise RuntimeError("Stale plugin tool result discarded")
            Draft202012Validator(tool.output_schema).validate(result)
            return result
        finally:
            current_call.reset(token)
            active.discard(current)
            if not active:
                self.active.pop(owner, None)


class InstalledPlugin:
    def __init__(self, definition, config, root, credential, capabilities, tools, *, resources=None):
        self.manifest = definition.manifest
        self.definition, self.config, self.root = definition, config, root
        self.credential, self.capabilities, self.tools = credential, capabilities, tools
        self.resources = resources or {}

    async def start(self, context):
        state = self.root / "state" / "plugins" / self.manifest.name
        state.mkdir(parents=True, mode=0o700, exist_ok=True)

        def subscribe(capacity):
            queue = context.runtime.subscribe(self.manifest.name, capacity)

            async def cleanup():
                context.runtime.unsubscribe(self.manifest.name)

            context.own(cleanup)
            return queue

        async def invoke(request, timeout=15):
            handler = context.runtime.handlers.get(type(request))
            if handler is None:
                raise RuntimeError("Requested capability unavailable")
            if handler[0] not in self.manifest.requires + self.manifest.optional:
                raise ValueError("Capability provider is not a declared dependency")
            from dataclasses import fields, replace, is_dataclass

            identity = current_call.get()
            if (
                identity
                and is_dataclass(request)
                and any(field.name == "caller_run" for field in fields(request))
            ):
                request = replace(request, caller_run=identity.task_run)
            return await context.runtime.invoke(request, timeout=timeout)

        def register(kind, handler):
            if kind not in self.definition.provides:
                raise ValueError("Undeclared plugin capability")
            context.register(kind, handler)

        public = PluginContext(
            name=self.manifest.name,
            config=self.definition.validate(copy.deepcopy(self.config))
            if self.definition.validate
            else copy.deepcopy(self.config),
            state_dir=state,
            get=context.get,
            set=context.set,
            credential=self.credential,
            invoke=invoke,
            publish=context.publish,
            subscribe=subscribe,
            spawn=context.spawn,
            own=context.own,
            cancelled=context.cancelled,
            capabilities=self.capabilities,
            resources=self.resources,
            register=register,
            readiness=context.ready,
            reporter=context.event_reporter,
            health=context.set_health,
            dropped=lambda: context.dropped,
        )

        def guard():
            if context.state not in {"ready", "degraded"}:
                raise RuntimeError("Plugin tool unavailable")
            return context.generation

        async def cleanup():
            await self.tools.close_owner(self.manifest.name)

        context.own(cleanup)
        self.tools.guards[self.manifest.name] = guard
        self.instance = self.definition.create(public)
        await self.instance.start()
        for tool in self.instance.tools() if hasattr(self.instance, "tools") else ():
            self.tools.register(self.manifest.name, tool)
        context.own(cleanup)  # Stop admitted calls before closing plugin resources.


def extension_credential(root, definition, name):
    """Only declared environment references are exposed, never a credential inventory."""
    import os
    from dotenv import dotenv_values

    if name not in definition.credentials or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,127}", name):
        raise ValueError("Undeclared plugin credential")
    if name in os.environ:
        return os.environ[name]
    path = root / ".env"
    if not path.exists():
        return None
    if path.stat().st_size > 65536:
        raise ValueError("Credential file too large")
    return dotenv_values(path, interpolate=False).get(name)
