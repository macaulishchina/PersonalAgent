from pathlib import Path
from assistant_runtime.api.plugins import plugin_platforms
from assistant_runtime.api import PluginDefinition, Manifest
from .settings import parse

from assistant_runtime.api.contracts import Trigger


def create(api):
    from .plugin import HidButtonsPlugin

    return HidButtonsPlugin(api.config)


DECLARATION = PluginDefinition(
    Manifest("hid_buttons", ("assistant",), critical=False),
    create=create,
    supported_platforms=plugin_platforms(Path(__file__).parent),
    uses=(Trigger,),
    config_fields=("hid_buttons",),
    config_key="hid_buttons",
    validate=parse,
)
