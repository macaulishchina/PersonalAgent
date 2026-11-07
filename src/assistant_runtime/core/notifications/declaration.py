from assistant_runtime.api.contracts import NotificationRequest, NotificationLink, TaskRequest
from assistant_runtime.api.files import FileRequest
from assistant_runtime.api.configuration import configuration_spec
from assistant_runtime.api import PluginDefinition, plugin_manifest
from .settings import NotificationSettings
from assistant_runtime.api.terminal import TerminalNotifications


def build(host):
    from .plugin import NotificationsPlugin

    return NotificationsPlugin(host, voice_host=host.assistant)


DECLARATION = PluginDefinition(
    provides=(
        NotificationRequest,
        NotificationLink,
        TerminalNotifications,
    ),
    uses=(TaskRequest, FileRequest),
    configuration=configuration_spec(__file__),
    manifest=plugin_manifest(__file__),
    internal_factory=build,
    config_key="notifications",
    validate=NotificationSettings.parse,
    service="notifications",
    tools_factory="assistant_runtime.core.notifications.tools:create_server",
)
