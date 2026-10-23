from assistant_runtime.api import PluginDefinition, Manifest
from .settings import parse

from assistant_runtime.api.contracts import Trigger


def create(api):
    from .plugin import HidButtonsPlugin

    return HidButtonsPlugin(api.config)


DECLARATION = PluginDefinition(
    Manifest("hid_buttons", ("assistant",), critical=False),
    create=create,
    uses=(Trigger,),
    config_fields=("hid_buttons",),
    config_key="hid_buttons",
    validate=parse,
)
