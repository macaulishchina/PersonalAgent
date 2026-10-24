from pathlib import Path
from assistant_runtime.api.plugins import plugin_platforms
from assistant_runtime.api import PluginDefinition, Manifest


def create(api):
    from .placeholder import VoicePlaceholder

    return VoicePlaceholder()


DECLARATION = PluginDefinition(
    supported_platforms=plugin_platforms(Path(__file__).parent),
    manifest=Manifest("voice_disabled", (), critical=False),
    create=create,
)
