from assistant_runtime.api import PluginDefinition, Manifest
from .settings import WebSettings

from assistant_runtime.api.contracts import TaskRequest, NotificationRequest


def create(api):
    from .plugin import WebPlugin

    return WebPlugin(api)


DECLARATION = PluginDefinition(
    Manifest("web", ("conversation",), critical=False),
    create=create,
    uses=(
        TaskRequest,
        NotificationRequest,
    ),
    resources=("conversation", "notifications_enabled", "tasks_enabled"),
    config_key="web",
    validate=WebSettings.parse,
)
