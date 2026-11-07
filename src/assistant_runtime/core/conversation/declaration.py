from assistant_runtime.api.files import FileRequest
from assistant_runtime.api.conversation import ConversationRequest, ConversationInputRequest
from assistant_runtime.api.contracts import ConversationFeedbackRequest, NotificationRequest, Control
from assistant_runtime.api import PluginDefinition, plugin_manifest


def build(host):
    from .plugin import Conversations

    return Conversations(host)


DECLARATION = PluginDefinition(
    provides=(
        ConversationRequest,
        ConversationInputRequest,
        ConversationFeedbackRequest,
    ),
    uses=(
        NotificationRequest,
        Control,
        FileRequest,
    ),
    manifest=plugin_manifest(__file__),
    internal_factory=build,
)
