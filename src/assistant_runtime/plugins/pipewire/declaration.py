from pathlib import Path
from assistant_runtime.api.configuration import configuration_spec
from assistant_runtime.api.plugins import plugin_platforms
from assistant_runtime.api.backends import plugin_backends
from assistant_runtime.api import PluginDefinition, plugin_manifest


def create(api):
    if api.backend == "wasapi":
        from .windows import WindowsAudioPlugin

        return WindowsAudioPlugin(
            api.config, api.resource("directory"), api.resource("audio_inputs"), api.resource("audio_outputs")
        )
    if api.backend != "pipewire":
        raise ValueError("No implementation for the selected local audio backend")
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
    plugin_manifest(__file__),
    configuration=configuration_spec(__file__),
    create=create,
    supported_platforms=plugin_platforms(Path(__file__).parent),
    backends=plugin_backends(Path(__file__).parent),
    resources=("state", "directory", "audio_outputs", "voice_binding", "audio_inputs"),
    config_fields=("input_device", "default_output", "minimum_volume", "echo_cancel"),
)
