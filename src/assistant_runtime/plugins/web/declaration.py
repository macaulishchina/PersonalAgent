from pathlib import Path
from assistant_runtime.api.plugins import plugin_platforms
from assistant_runtime.api.configuration import configuration_spec
from assistant_runtime.api import PluginDefinition, Manifest
from .settings import WebSettings

from assistant_runtime.api.contracts import TaskRequest, NotificationRequest
from assistant_runtime.api.conversation import ConversationRequest


def create(api):
    from .plugin import WebPlugin

    return WebPlugin(api)


DECLARATION = PluginDefinition(
    supported_platforms=plugin_platforms(Path(__file__).parent),
    configuration=configuration_spec(__file__),
    manifest=Manifest("web", ("conversation",), critical=False),
    create=create,
    uses=(
        ConversationRequest,
        TaskRequest,
        NotificationRequest,
    ),
    resources=("conversation", "notifications_enabled", "tasks_enabled"),
    config_key="web",
    validate=WebSettings.parse,
)
