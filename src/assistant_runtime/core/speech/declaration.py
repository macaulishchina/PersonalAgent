from assistant_runtime.api import PluginDefinition, Manifest


def build(host):
    from .plugin import AssistantPlugin

    return AssistantPlugin(host.assistant, DECLARATION.manifest.requires)


DECLARATION = PluginDefinition(
    Manifest("assistant", ("conversation", "devices", "voice_disabled"), critical=True),
    internal_factory=build,
)
