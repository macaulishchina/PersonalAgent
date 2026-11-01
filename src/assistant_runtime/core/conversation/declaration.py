from assistant_runtime.api.conversation import ConversationRequest
from assistant_runtime.api.contracts import ConversationFeedbackRequest, NotificationRequest, Control
from assistant_runtime.api import PluginDefinition, plugin_manifest


def build(host):
    from .plugin import Conversations

    return Conversations(host)


DECLARATION = PluginDefinition(
    provides=(
        ConversationRequest,
        ConversationFeedbackRequest,
    ),
    uses=(
        NotificationRequest,
        Control,
    ),
    manifest=plugin_manifest(__file__),
    internal_factory=build,
)
