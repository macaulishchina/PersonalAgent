"""Explicit installed-extension discovery and owned public-context construction."""

from assistant_runtime.api.configuration_io import configuration_path

import asyncio
import copy
from dataclasses import replace
import logging
from importlib.metadata import entry_points
import re

from assistant_runtime.api import CallIdentity, PluginContext, PluginDefinition
from assistant_runtime.api.plugins import current_call, require_platform, PlatformCertificationError
from .plugin_metadata import installed_platforms, installed_backends, installed_release
from assistant_runtime.core.configuration.schema import resolve_definition

GROUP = "personal_agent.plugins"
log = logging.getLogger(__name__)


def installed_entries(names, builtins=()):
    """Resolve distribution identities without importing any extension code."""
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
    for name in names:
        if name not in found:
            raise ValueError("Plugin not installed: " + name)
    return found


def discover(names, builtins=()):
    """Read metadata without importing disabled extensions; reject ambiguous ownership."""
    found = installed_entries(names, builtins)
    result = {}
    for name in names:
        from assistant_runtime.api.system import system

        try:
            platforms = installed_platforms(found[name])
        except ValueError as exc:
            raise PlatformCertificationError(
                f"Plugin {name} cannot enable on {system.name}; certified platforms: none "
                "(missing or invalid static platform certification)"
            ) from exc
        require_platform(name, platforms, system.name)
        backends = installed_backends(found[name])
        from assistant_runtime.app.versions import CompatibilityError, compatibility, manifest

        try:
            release = installed_release(found[name])
        except (ValueError, OSError, TypeError, KeyError) as exc:
            raise CompatibilityError(
                f"Plugin {name}: missing or invalid release declaration; update the plugin"
            ) from exc
        error = compatibility(release)
        if error:
            raise CompatibilityError(error)
        definition = found[name].load()
        definition = definition() if callable(definition) else definition
        if not isinstance(definition, PluginDefinition) or definition.manifest.name != name:
            raise ValueError("Invalid plugin declaration: " + name)
        if definition.internal_factory is not None or definition.create is None:
            raise ValueError("Installed plugins require a public-context factory")
        if set(definition.resources) - {"system_io", "execution_control"}:
            raise ValueError(
                "Installed plugin requested unsupported direct resources; use declared capabilities"
            )
        if "execution_control" in definition.resources and "execution" not in definition.manifest.requires:
            raise ValueError("Execution control requires the execution dependency")
        if set(definition.supported_platforms) != set(platforms):
            raise PlatformCertificationError(
                f"Plugin {name} cannot enable on {system.name}; certified platforms: {', '.join(platforms)}; "
                "static and runtime platform certifications differ"
            )
        if definition.backends != backends:
            raise ValueError(f"Plugin {name}: static and runtime backend declarations differ")
        if definition.manifest != manifest(release):
            raise CompatibilityError(f"Plugin {name}: static and runtime release declarations differ")
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

        if not isinstance(tool.name, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,95}", tool.name):
            raise ValueError("Invalid tool declaration")
        if tool.name in self.tools:
            raise ValueError("Duplicate tool: " + tool.name)
        if not callable(tool.handler):
            raise ValueError("Tool requires a callable handler")
        warnings = []
        description = tool.description.strip() if isinstance(tool.description, str) else ""
        if not description:
            description = tool.name
            warnings.append("missing_description")
        flags = {}
        for key, fallback in (
            ("read_only", False),
            ("destructive", True),
            ("idempotent", False),
            ("external", True),
        ):
            value = getattr(tool, key)
            flags[key] = value if type(value) is bool else fallback
            if type(value) is not bool:
                warnings.append("invalid_" + key)
        if flags["read_only"] and flags["destructive"]:
            flags["read_only"] = False
            warnings.append("conflicting_behavior_hints")
        schemas = []
        for label, source in (("input", tool.input_schema), ("output", tool.output_schema)):
            schema = copy.deepcopy(dict(source))
            # Examples are documentation, not business constraints. Never let a
            # broken example prevent an otherwise valid tool from running, nor
            # send that example to the model as guidance.
            examples = schema.pop("examples", None)
            Draft202012Validator.check_schema(schema)
            if examples is not None:
                if isinstance(examples, list):
                    validator = Draft202012Validator(schema)
                    valid = [example for example in examples if validator.is_valid(example)]
                    if len(valid) != len(examples):
                        warnings.append("invalid_" + label + "_examples")
                    if valid:
                        schema["examples"] = valid
                else:
                    warnings.append("invalid_" + label + "_examples")
            schemas.append(schema)
        # Registration fixes one contract version. A caller must not accidentally
        # mutate a live declaration through its original dict or a description.
        self.tools[tool.name] = (
            owner,
            replace(
                tool,
                description=description,
                input_schema=schemas[0],
                output_schema=schemas[1],
                **flags,
            ),
        )
        if warnings:
            log.warning(
                "Tool declaration quality owner=%s tool=%s issues=%s; compatible registration retained",
                owner,
                tool.name,
                ",".join(warnings),
            )

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
                    "input_schema": copy.deepcopy(dict(tool.input_schema)),
                    "output_schema": copy.deepcopy(dict(tool.output_schema)),
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
        from jsonschema.exceptions import ValidationError
        from assistant_runtime.api.tool_calls import ToolFailure, ToolOutcome
        import time

        try:
            owner, tool = self.tools[name]
            guard = self.guards.get(owner)
            generation = guard() if guard else None
        except (KeyError, RuntimeError):
            raise ToolFailure(
                ToolOutcome(
                    "tool_unavailable",
                    "preparation",
                    "not_dispatched",
                    "failed",
                    "Tool owner is unavailable",
                    next_action="inspect_definition",
                )
            ) from None
        from assistant_runtime.mcp.contracts import validate

        validate(arguments, tool.input_schema, "")
        if identity.deadline is not None:
            timeout = (
                (identity.deadline - time.monotonic())
                if timeout is None
                else min(timeout, identity.deadline - time.monotonic())
            )
        if timeout is not None and timeout <= 0:
            raise ToolFailure(
                ToolOutcome(
                    "deadline_expired",
                    "preparation",
                    "not_dispatched",
                    "cancelled",
                    "Request deadline exceeded",
                    next_action="inspect_request",
                )
            )
        token = current_call.set(identity)
        current = asyncio.current_task()
        active = self.active.setdefault(owner, set())
        active.add(current)
        try:
            try:
                result = await asyncio.wait_for(tool.handler(copy.deepcopy(arguments)), timeout)
            except ToolFailure:
                raise
            except Exception as exc:
                raise ToolFailure(
                    ToolOutcome(
                        "tool_result_unconfirmed",
                        "return",
                        "dispatched",
                        "unknown",
                        "Tool handler did not return a confirmed result; inspect the operation before repeating it",
                        next_action="inspect_operation",
                    )
                ) from exc
            try:
                stale = guard and guard() != generation
            except RuntimeError:
                stale = True
            if stale:
                raise ToolFailure(
                    ToolOutcome(
                        "stale_tool_result",
                        "result",
                        "dispatched",
                        "unknown",
                        "Plugin owner changed after dispatch; inspect the received result without repeating the operation",
                        next_action="inspect_operation",
                    ),
                    result=result,
                )
            try:
                Draft202012Validator(tool.output_schema).validate(result)
            except ValidationError:
                raise ToolFailure(
                    ToolOutcome(
                        "invalid_tool_result",
                        "result",
                        "dispatched",
                        "unknown",
                        "Tool returned an invalid result. The operation may have completed; do not repeat it.",
                        next_action="inspect_operation",
                    ),
                    result=result,
                ) from None
            return result
        finally:
            current_call.reset(token)
            active.discard(current)
            if not active:
                self.active.pop(owner, None)


class InstalledPlugin:
    def __getattr__(self, name):
        if name == "configuration_action" and "instance" in self.__dict__:
            return getattr(self.instance, name)
        if name != "prepare_configuration" or "instance" not in self.__dict__:
            raise AttributeError(name)
        callback = getattr(self.instance, name)

        async def prepare(config):
            from assistant_runtime.api.configuration import scoped_update

            value = resolve_definition(self.definition, config.plugin_settings.get(self.manifest.name, {}))
            return scoped_update(await callback(value), self.public, value)

        return prepare

    def __init__(
        self,
        definition,
        config,
        root,
        credential,
        capabilities,
        tools,
        *,
        resources=None,
        backend_defaults=None,
    ):
        self.manifest = definition.manifest
        self.context = None
        self.definition, self.config, self.root = definition, config, root
        self.credential, self.capabilities, self.tools = credential, capabilities, tools
        self.backend_defaults = backend_defaults or {}
        self.resources = resources or {}
        if "system_io" in definition.resources:
            from assistant_runtime.api.system import system

            self.resources["system_io"] = system

    def maintenance_pause(self):
        callback = getattr(getattr(self, "instance", None), "maintenance_pause", None)
        if callback:
            callback()

    def maintenance_resume(self):
        callback = getattr(getattr(self, "instance", None), "maintenance_resume", None)
        if callback:
            callback()

    def maintenance_pending(self):
        callback = getattr(getattr(self, "instance", None), "maintenance_pending", None)
        if callback:
            return callback()
        return [{"state": "drain_not_supported"}] if self.context and self.context.tasks else []

    async def start(self, context):
        self.context = context
        state = self.root / "state" / "plugins" / self.manifest.name
        state.mkdir(parents=True, mode=0o700, exist_ok=True)

        def subscribe(capacity):
            queue = context.runtime.subscribe(self.manifest.name, capacity)

            async def cleanup():
                context.runtime.unsubscribe(self.manifest.name)

            context.own(cleanup)
            return queue

        async def invoke(request, timeout=None):
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

        def register(kind, handler, *, timeout=15):
            if kind not in self.definition.provides:
                raise ValueError("Undeclared plugin capability")
            context.register(kind, handler, timeout=timeout)

        def configuration_resource(name):
            from assistant_runtime.api.configuration_io import configuration_path

            specs = self.definition.configuration.resources if self.definition.configuration else ()
            resource = next((r for r in specs if r["id"] == name), None)
            if resource is None:
                raise ValueError("Undeclared configuration resource")
            base = self.root / "config/plugins" / self.manifest.name
            path = base / resource["path"]
            if not path.resolve().is_relative_to(base.resolve()) or any(
                p.is_symlink() for p in [path, *path.parents]
            ):
                raise ValueError("Invalid configuration resource path")
            return configuration_path(path)

        from assistant_runtime.api.backends import select_backend

        public = self.public = PluginContext(
            available=lambda request: context.runtime.available(request),
            platform=context.platform,
            backend=select_backend(
                self.manifest.name, self.definition.backends, context.platform.name, self.backend_defaults
            ),
            configuration_resource=configuration_resource,
            name=self.manifest.name,
            config=resolve_definition(self.definition, self.config),
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
    path = configuration_path(root / ".env")
    if not path.exists():
        return None
    if path.stat().st_size > 65536:
        raise ValueError("Credential file too large")
    return dotenv_values(configuration_path(path), interpolate=False).get(name)
