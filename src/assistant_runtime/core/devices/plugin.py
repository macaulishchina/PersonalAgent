"""Device resources have one owner, independent of application composition."""

from assistant_runtime.api.contracts import VoiceBinding
from .directory import DeviceDirectory, DevicesPlugin
from .resources import DeviceResources
from .operations import DeviceOperations
from .outputs import AudioOutputs
from .inputs import AudioInputs
from .video import VideoInputs
from .cameras import CameraControls


class DeviceServices:
    def __init__(self, config):
        self.voice_io = VoiceBinding(config.input_device, config.default_output)
        self.devices = DeviceDirectory(config.devices, self.voice_io)
        self.device_resources = DeviceResources()
        self.device_operations = DeviceOperations(self.devices, self.device_resources)
        self.audio_outputs = AudioOutputs(config.devices, self.device_resources)
        self.audio_inputs = AudioInputs(config.devices, self.device_resources)
        self.video_inputs = VideoInputs(config.devices)
        self.camera_controls = CameraControls(config.devices)
        self.plugin = DevicesPlugin(
            self.devices,
            self.video_inputs,
            self.camera_controls,
            self.audio_inputs,
            self.audio_outputs,
        )

    def bind(self, host):
        """Core composition handles; extension contexts still receive scoped resources."""
        for name in (
            "voice_io",
            "devices",
            "device_resources",
            "device_operations",
            "audio_outputs",
            "audio_inputs",
            "video_inputs",
            "camera_controls",
        ):
            setattr(host, name, getattr(self, name))
