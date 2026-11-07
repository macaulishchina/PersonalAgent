"""Composition root. This is the only module that chooses concrete plugins."""

from assistant_runtime.kernel.runtime import Runtime


def assistant_skill_paths(config):
    """The composition root selects bundled resources from enabled owners only."""
    from assistant_runtime.app.skills import instance_skill_paths

    from .plugin_resources import skill_paths

    paths = skill_paths(config.plugins)
    private = instance_skill_paths(config.root, config.instance_skills)
    if {path.name for path in paths} & {path.name for path in private}:
        raise ValueError("Instance skills must not shadow bundled assistant skills")
    return paths + private


def compose(service):
    """Construct declared owners in dependency order before starting any work."""
    from types import SimpleNamespace
    from .bundled import create, resources_for
    from .extensions import discover, InstalledPlugin, ToolRegistry, extension_credential
    from assistant_runtime.core.plugin_catalog import OWNERS, declaration
    from assistant_runtime.api.system import system

    config, store = service.config, service.store
    selected = config.enabled_plugins
    from assistant_runtime.api import PlatformInfo
    from .plugin_selection import resolve_selection

    platform = PlatformInfo(system.name)
    resolve_selection(selected, config.plugin_backends, platform.name)
    definitions = {name: declaration(name) for name in selected if name in OWNERS}
    definitions.update(discover([name for name in selected if name not in OWNERS], OWNERS))
    graph = Runtime(store, platform=platform)
    for name, spec in definitions.items():
        graph.add(SimpleNamespace(manifest=spec.manifest))
    order = graph.order()  # Reject missing/cyclic dependencies before constructing resources.
    runtime = service.runtime = Runtime(
        store, {k: v for k, v in config.bindings.items() if v in selected}, platform=platform
    )

    service.mcp_registry = None
    if "integrations" in selected:
        from assistant_runtime.core.integrations.registry import McpRegistry

        service.mcp_registry = McpRegistry(config.root, config.external_mcp)
    if "devices" in selected:
        from assistant_runtime.core.devices.plugin import DeviceServices

        service.device_services = DeviceServices(config)
        service.device_services.bind(service)
    if "assistant" in selected:
        from assistant_runtime.core.speech.state import AssistantState

        service.assistant = AssistantState(service)
    service.skill_paths = assistant_skill_paths(config)
    service.extension_tools = ToolRegistry()
    # A narrow read-only convention, not an arbitrary plugin-method dispatcher.
    service.operation_sources = {
        name: spec.operation_request for name, spec in definitions.items() if spec.operation_request
    }
    service.memory = None
    for name in order:
        spec = definitions[name]
        if spec.internal_factory:
            if name not in OWNERS or not OWNERS[name].startswith("assistant_runtime.core."):
                raise ValueError("Only framework core owners can use internal services")
            plugin = spec.internal_factory(service)
        elif name in OWNERS:
            plugin = create(service, name)
        else:
            plugin = InstalledPlugin(
                spec,
                config.plugin_settings.get(name, {}),
                config.root,
                lambda key, spec=spec: extension_credential(config.root, spec, key),
                spec.uses,
                service.extension_tools,
                resources=resources_for(service, name, spec.resources),
                backend_defaults=config.plugin_backends.get(name, {}),
            )
        if plugin.manifest.name != name:
            raise ValueError("Plugin manifest name must match its registry name")
        if (plugin.manifest.version, plugin.manifest.api_version) != (
            spec.manifest.version,
            spec.manifest.api_version,
        ):
            from .versions import CompatibilityError

            raise CompatibilityError(
                f"Plugin {name}: constructed version differs from its release declaration"
            )
        runtime.add(plugin)
    service.memory = runtime.plugins.get(config.bindings.get("memory"))
    if getattr(service, "backend", None):
        service.backend.on_task_done = service.memory.enqueue if service.memory else None
    if service.assistant is not None:
        from assistant_runtime.core.speech.disabled import DisabledVoice

        state = service.assistant
        service.audio_plugin = runtime.resolve("audio") if runtime.bindings.get("audio") else None
        state.route = getattr(service.audio_plugin, "route", None)
        state.player = getattr(service.audio_plugin, "player", None)
        state.recognizer = runtime.resolve("trigger").recognizer
        state.interaction = None
        # Conversation construction binds callbacks before startup.
        state.voice = getattr(state, "voice", None) or DisabledVoice()
    from .plugin_inventory import persist

    persist(config)
    return runtime
