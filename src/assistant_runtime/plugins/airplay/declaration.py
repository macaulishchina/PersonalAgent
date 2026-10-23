from assistant_runtime.api import PluginDefinition, Manifest


def create(api):
    from .plugin import AirPlayPlugin

    return AirPlayPlugin(
        api.config, api.resource("directory"), api.resource("audio_outputs"), api.resource("voice_binding")
    )


DECLARATION = PluginDefinition(
    Manifest("airplay", (), critical=False),
    create=create,
    resources=("directory", "audio_outputs", "voice_binding"),
    config_fields=(),
)
