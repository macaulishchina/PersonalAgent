from assistant_runtime.api import PluginDefinition, Manifest


def create(api):
    from .plugin import Go2RTCPlugin

    return Go2RTCPlugin(
        api.config, api.resource("directory"), api.resource("video_inputs"), api.resource("camera_controls")
    )


DECLARATION = PluginDefinition(
    Manifest("go2rtc", (), critical=False),
    create=create,
    resources=("directory", "video_inputs", "camera_controls"),
    config_fields=("state_dir",),
)
