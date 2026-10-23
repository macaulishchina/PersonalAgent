from assistant_runtime.api import PluginDefinition, Manifest
from assistant_runtime.api.contracts import Trigger


def create(api):
    from .gesture import GesturePlugin

    return GesturePlugin()


DECLARATION = PluginDefinition(Manifest("gesture", (), critical=False), create=create, uses=(Trigger,))
