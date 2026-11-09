from pathlib import Path
from assistant_runtime.api.plugins import plugin_platforms
from assistant_runtime.api.configuration import configuration_spec
from assistant_runtime.api import PluginDefinition, plugin_manifest
from .settings import AppleHomeSettings


def create(api):
    from .plugin import AppleHomePlugin

    return AppleHomePlugin(api.config, api.resource("state"), api.resource("directory"))


DECLARATION = PluginDefinition(
    supported_platforms=plugin_platforms(Path(__file__).parent),
    configuration=configuration_spec(__file__),
    manifest=plugin_manifest(__file__),
    create=create,
    resources=("state", "directory"),
    config_fields=("state_dir",),
    validate=AppleHomeSettings.parse,
)
