from pathlib import Path
from assistant_runtime.api.configuration import configuration_spec
from assistant_runtime.api.plugins import plugin_platforms
from assistant_runtime.api import PluginDefinition, plugin_manifest


def create(api):
    from .plugin import KeywordPlugin

    return KeywordPlugin(api.config)


DECLARATION = PluginDefinition(
    supported_platforms=plugin_platforms(Path(__file__).parent),
    manifest=plugin_manifest(__file__),
    configuration=configuration_spec(__file__),
    create=create,
    config_fields=("models", "wake_words"),
)
