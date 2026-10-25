from assistant_runtime.api import PluginDefinition, Manifest
from assistant_runtime.api.configuration import configuration_spec


def build(host):
    from .plugin import AssistantPlugin

    return AssistantPlugin(host.assistant, DECLARATION.manifest.requires)


DECLARATION = PluginDefinition(
    Manifest("assistant", ("conversation", "devices", "voice_disabled"), critical=True),
    internal_factory=build,
    configuration=configuration_spec(__file__),
)
