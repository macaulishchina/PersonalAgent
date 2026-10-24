from pathlib import Path
from assistant_runtime.api.plugins import plugin_platforms
from assistant_runtime.api.configuration import configuration_spec
from assistant_runtime.api import PluginDefinition, Manifest
from .settings import AppleHomeSettings


def create(api):
    from .plugin import AppleHomePlugin

    return AppleHomePlugin(api.config, api.resource("state"), api.resource("directory"))


DECLARATION = PluginDefinition(
    supported_platforms=plugin_platforms(Path(__file__).parent),
    configuration=configuration_spec(__file__),
    manifest=Manifest("apple_home", (), critical=False),
    create=create,
    resources=("state", "directory"),
    config_fields=("apple_home", "state_dir"),
    config_key="apple_home",
    validate=AppleHomeSettings.parse,
)
