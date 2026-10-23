from assistant_runtime.api import PluginDefinition, Manifest
from .settings import MediaSettings
from pathlib import Path

from assistant_runtime.api.contracts import TaskRequest, NotificationRequest, NotificationLink, MediaRequest


def create(api):
    from .plugin import MediaPlugin

    return MediaPlugin(api)


DECLARATION = PluginDefinition(
    Manifest("media", ("notifications",), critical=False),
    create=create,
    uses=(
        TaskRequest,
        NotificationRequest,
        NotificationLink,
    ),
    provides=(MediaRequest,),
    config_key="media",
    validate=MediaSettings.parse,
    skills=(Path(__file__).parent / "skills" / "media-acquisition",),
    service="media",
    tools_factory="assistant_runtime.plugins.media.tools:create_server",
)
