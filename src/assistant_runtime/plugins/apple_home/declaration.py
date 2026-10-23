from assistant_runtime.api import PluginDefinition, Manifest
from .settings import AppleHomeSettings


def create(api):
    from .plugin import AppleHomePlugin

    return AppleHomePlugin(api.config, api.resource("state"), api.resource("directory"))


DECLARATION = PluginDefinition(
    Manifest("apple_home", (), critical=False),
    create=create,
    resources=("state", "directory"),
    config_fields=("apple_home", "state_dir"),
    config_key="apple_home",
    validate=AppleHomeSettings.parse,
)
