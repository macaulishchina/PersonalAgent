from assistant_runtime.api import PluginDefinition, Manifest
from .settings import NotificationSettings


def build(host):
    from .plugin import NotificationsPlugin

    return NotificationsPlugin(host, voice_host=host.assistant)


DECLARATION = PluginDefinition(
    Manifest("notifications", optional=("conversation",), critical=False),
    internal_factory=build,
    config_key="notifications",
    validate=NotificationSettings.parse,
    service="notifications",
    tools_factory="assistant_runtime.core.notifications.tools:create_server",
)
