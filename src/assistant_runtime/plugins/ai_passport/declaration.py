"""Static declaration without hardware or firmware-toolchain imports."""

from pathlib import Path

from assistant_runtime.api import PluginDefinition, plugin_manifest
from assistant_runtime.api.configuration import configuration_spec
from assistant_runtime.api.plugins import plugin_platforms


def create(context):
    from .plugin import AiPassportPlugin

    return AiPassportPlugin()


DECLARATION = PluginDefinition(
    manifest=plugin_manifest(__file__),
    supported_platforms=plugin_platforms(Path(__file__).parent),
    configuration=configuration_spec(__file__),
    create=create,
)
