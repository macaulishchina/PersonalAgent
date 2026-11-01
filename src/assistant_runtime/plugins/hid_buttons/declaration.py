from pathlib import Path
from assistant_runtime.api.plugins import plugin_platforms
from assistant_runtime.api import PluginDefinition, plugin_manifest
from assistant_runtime.api.configuration import configuration_spec
from .settings import parse

from assistant_runtime.api.contracts import Trigger


def create(api):
    from .plugin import HidButtonsPlugin

    return HidButtonsPlugin(api.config)


DECLARATION = PluginDefinition(
    manifest=plugin_manifest(__file__),
    configuration=configuration_spec(__file__),
    create=create,
    supported_platforms=plugin_platforms(Path(__file__).parent),
    uses=(Trigger,),
    config_fields=("hid_buttons",),
    config_key="hid_buttons",
    validate=parse,
)
