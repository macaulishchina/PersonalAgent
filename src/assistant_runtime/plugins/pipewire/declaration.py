from assistant_runtime.api import PluginDefinition, Manifest


def create(api):
    from .plugin import PipeWirePlugin

    return PipeWirePlugin(
        api.config,
        api.resource("state"),
        api.resource("directory"),
        api.resource("audio_outputs"),
        api.resource("voice_binding"),
        api.resource("audio_inputs"),
    )


DECLARATION = PluginDefinition(
    Manifest("pipewire", (), critical=False),
    create=create,
    resources=("state", "directory", "audio_outputs", "voice_binding", "audio_inputs"),
    config_fields=("input_device", "default_output", "minimum_volume", "echo_cancel"),
)
