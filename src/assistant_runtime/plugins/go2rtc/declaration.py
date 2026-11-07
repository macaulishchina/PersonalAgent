from pathlib import Path
from assistant_runtime.api.plugins import plugin_platforms
from assistant_runtime.api import PluginDefinition, plugin_manifest
from assistant_runtime.api.configuration import configuration_spec


def create(api):
    from .plugin import Go2RTCPlugin

    return Go2RTCPlugin(
        api.config, api.resource("directory"), api.resource("video_inputs"), api.resource("camera_controls")
    )


DECLARATION = PluginDefinition(
    supported_platforms=plugin_platforms(Path(__file__).parent),
    manifest=plugin_manifest(__file__),
    configuration=configuration_spec(__file__),
    create=create,
    resources=("directory", "video_inputs", "camera_controls"),
    config_fields=("state_dir",),
)
