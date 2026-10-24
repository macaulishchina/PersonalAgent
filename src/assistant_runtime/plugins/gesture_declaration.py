from pathlib import Path
from assistant_runtime.api.plugins import plugin_platforms
from assistant_runtime.api import PluginDefinition, Manifest
from assistant_runtime.api.contracts import Trigger


def create(api):
    from .gesture import GesturePlugin

    return GesturePlugin()


DECLARATION = PluginDefinition(
    supported_platforms=plugin_platforms(Path(__file__).with_name("gesture.platform.json")),
    manifest=Manifest("gesture", (), critical=False),
    create=create,
    uses=(Trigger,),
)
