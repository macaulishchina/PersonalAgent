from pathlib import Path
from assistant_runtime.api.plugins import plugin_platforms
from assistant_runtime.api.configuration import configuration_spec
from assistant_runtime.api import PluginDefinition, plugin_manifest
from .settings import MediaSettings

from assistant_runtime.api.contracts import TaskRequest, NotificationRequest, NotificationLink, MediaRequest


def create(api):
    from .plugin import MediaPlugin

    return MediaPlugin(api)


DECLARATION = PluginDefinition(
    supported_platforms=plugin_platforms(Path(__file__).parent),
    configuration=configuration_spec(__file__),
    manifest=plugin_manifest(__file__),
    create=create,
    uses=(
        TaskRequest,
        NotificationRequest,
        NotificationLink,
    ),
    provides=(MediaRequest,),
    operation_request=MediaRequest,
    config_key="media",
    validate=MediaSettings.parse,
    skills=(Path(__file__).parent / "skills" / "media-acquisition",),
    service="media",
    tools_factory="assistant_runtime.plugins.media.tools:create_server",
)
