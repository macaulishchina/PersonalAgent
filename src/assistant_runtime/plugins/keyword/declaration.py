from assistant_runtime.api import PluginDefinition, Manifest


def create(api):
    from .plugin import KeywordPlugin

    return KeywordPlugin(api.config)


DECLARATION = PluginDefinition(
    Manifest("keyword", (), critical=False),
    create=create,
    config_fields=("models", "wake_words"),
)
