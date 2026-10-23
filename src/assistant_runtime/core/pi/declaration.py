from assistant_runtime.api import PluginDefinition, Manifest


def build(host):
    from .plugin import PiPlugin

    assistant = host.assistant
    plugin = PiPlugin(
        host.config,
        host.store,
        assistant.speak if assistant else None,
        assistant.tool_control if assistant else None,
        host.mcp_registry,
        host.ai_usage.service,
        skill_paths=host.skill_paths,
        foreground="conversation" in host.config.enabled_plugins,
    )
    host.backend = plugin.backend
    host.brain_plugin = plugin
    return plugin


DECLARATION = PluginDefinition(
    Manifest("pi", ("ai_usage",), critical=True),
    internal_factory=build,
)
