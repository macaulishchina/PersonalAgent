from pathlib import Path
from assistant_runtime.api.plugins import plugin_platforms
from assistant_runtime.api import PluginDefinition, Manifest


def create(api):
    from .plugin import V4L2Plugin

    return V4L2Plugin(api.config, api.resource("directory"), api.resource("video_inputs"))


DECLARATION = PluginDefinition(
    Manifest("v4l2", (), critical=False),
    create=create,
    supported_platforms=plugin_platforms(Path(__file__).parent),
    resources=("directory", "video_inputs"),
    config_fields=(),
)
