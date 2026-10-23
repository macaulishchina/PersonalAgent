from assistant_runtime.api import PluginDefinition, Manifest


def build(host):
    from .service import Conversation

    host.conversation = Conversation(host.assistant)
    return host.conversation


DECLARATION = PluginDefinition(
    Manifest("conversation", ("assistant", "workfiles", "execution"), critical=True),
    internal_factory=build,
)
