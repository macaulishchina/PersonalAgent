from pathlib import Path
from assistant_runtime.api.plugins import plugin_platforms
from assistant_runtime.api import PluginDefinition, Manifest


def create(api):
    from .plugin import KeywordPlugin

    return KeywordPlugin(api.config)


DECLARATION = PluginDefinition(
    supported_platforms=plugin_platforms(Path(__file__).parent),
    manifest=Manifest("keyword", (), critical=False),
    create=create,
    config_fields=("models", "wake_words"),
)
