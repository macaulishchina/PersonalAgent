from assistant_runtime.api import PluginDefinition, Manifest


def create(api):
    from .placeholder import VoicePlaceholder

    return VoicePlaceholder()


DECLARATION = PluginDefinition(Manifest("voice_disabled", (), critical=False), create=create)
