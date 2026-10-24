from assistant_runtime.api import PluginDefinition, Manifest


def build(host):
    from .plugin import Conversations

    return Conversations(host)


DECLARATION = PluginDefinition(
    Manifest("conversation", ("workfiles", "execution"), critical=True),
    internal_factory=build,
)
