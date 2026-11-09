from pathlib import Path
from assistant_runtime.api.configuration import configuration_spec
from assistant_runtime.api.plugins import plugin_platforms
from assistant_runtime.api.backends import plugin_backends
from assistant_runtime.api import PluginDefinition, plugin_manifest


def create(api):
    if api.backend == "directshow":
        from .windows import WindowsCameraPlugin

        return WindowsCameraPlugin(api.config, api.resource("directory"), api.resource("video_inputs"))
    if api.backend != "v4l2":
        raise ValueError("No implementation for the selected local camera backend")
    from .plugin import V4L2Plugin

    return V4L2Plugin(api.config, api.resource("directory"), api.resource("video_inputs"))


DECLARATION = PluginDefinition(
    plugin_manifest(__file__),
    configuration=configuration_spec(__file__),
    create=create,
    supported_platforms=plugin_platforms(Path(__file__).parent),
    backends=plugin_backends(Path(__file__).parent),
    resources=("directory", "video_inputs"),
    config_fields=(),
)
