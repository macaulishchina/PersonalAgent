from pathlib import Path
from assistant_runtime.api.plugins import plugin_platforms
from assistant_runtime.api import PluginDefinition, plugin_manifest
from assistant_runtime.api.contracts import Trigger


def create(api):
    from .gesture import GesturePlugin

    return GesturePlugin()


DECLARATION = PluginDefinition(
    supported_platforms=plugin_platforms(Path(__file__).with_name("gesture.plugin.json")),
    manifest=plugin_manifest(__file__),
    create=create,
    uses=(Trigger,),
)
