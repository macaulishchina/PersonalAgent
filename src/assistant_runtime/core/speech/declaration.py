from assistant_runtime.api import PluginDefinition, plugin_manifest
from assistant_runtime.api.configuration import configuration_spec


def build(host):
    from .plugin import AssistantPlugin

    return AssistantPlugin(host.assistant, DECLARATION.manifest.requires)


DECLARATION = PluginDefinition(
    plugin_manifest(__file__),
    internal_factory=build,
    configuration=configuration_spec(__file__),
)
