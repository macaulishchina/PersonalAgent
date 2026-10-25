from assistant_runtime.api import PluginDefinition, plugin_manifest


def build(host):
    from .plugin import Conversations

    return Conversations(host)


DECLARATION = PluginDefinition(
    plugin_manifest(__file__),
    internal_factory=build,
)
