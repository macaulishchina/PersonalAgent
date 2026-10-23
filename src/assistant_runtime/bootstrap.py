"""Composition root. This is the only module that chooses concrete plugins."""

from .kernel.runtime import Runtime
from .kernel.contracts import Control, Trigger, VoiceBinding


def assistant_skill_paths(config):
    """The composition root selects bundled resources from enabled owners only."""
    from pathlib import Path
    from .assistant.skills import instance_skill_paths

    plugins = Path(__file__).parent / "plugins"
    bundled = (
        ("media", "media-acquisition"),
        ("tasks", "event-driven-tasks"),
        ("events", "event-management"),
    )
    paths = tuple(plugins / owner / "skills" / name for owner, name in bundled if owner in config.plugins)
    for path in paths:
        if not (path / "SKILL.md").is_file():
            raise ValueError(f"Bundled assistant skill is missing: {path.name}")
    private = instance_skill_paths(config.root, config.instance_skills)
    if {path.name for path in paths} & {path.name for path in private}:
        raise ValueError("Instance skills must not shadow bundled assistant skills")
    return paths + private


async def memory_complete(service, messages, schema):
    from .plugins.models.settings import ModelSettings
    from .kernel.models import ModelRequest
    import json
    import uuid

    binding = ModelSettings.parse(service.config.model_api).memory
    if not binding:
        raise RuntimeError("Memory requires an explicit model_api.memory binding")
    from .kernel.ai_usage import current_usage

    token = current_usage.set(current_usage.get() or await service.ai_usage.scope(independent="memory"))
    try:
        result = await service.runtime.invoke(
            ModelRequest(
                "infer",
                {
                    "request_id": str(uuid.uuid4()),
                    "model": binding,
                    "text": json.dumps(messages, ensure_ascii=False),
                    "schema": schema,
                },
            ),
            timeout=90,
        )
    finally:
        current_usage.reset(token)
    if result.get("state") != "completed":
        raise RuntimeError("Memory model result unconfirmed")
    return result["data"]


def compose(service):
    from .plugins.pipewire.plugin import PipeWirePlugin
    from .plugins.speech.placeholder import VoicePlaceholder
    from .plugins.pi.plugin import PiPlugin
    from .plugins.gesture import GesturePlugin
    from .plugins.hid_buttons.plugin import HidButtonsPlugin
    from .memory.service import MemoryPlugin
    from .memory.mem0 import Mem0Memory

    from .devices.directory import DeviceDirectory, DevicesPlugin

    config, store = service.config, service.store
    from .workfiles import WorkFiles, WorkFilesMaintenance

    service.workfiles = WorkFiles(config.state_dir, store.db, config.work_files)
    from .plugins.integrations.registry import McpRegistry

    service.mcp_registry = (
        McpRegistry(config.root, config.external_mcp) if "integrations" in config.plugins else None
    )

    def integrations():
        from .plugins.integrations.plugin import IntegrationsPlugin

        return IntegrationsPlugin(service, service.mcp_registry, lambda: service.backend.integrations)

    def events():
        from .plugins.events.plugin import EventsPlugin

        return EventsPlugin(service)

    def observations():
        from .plugins.observations.plugin import ObservationsPlugin
        from .plugins.reyee.events import ReyeeEvents
        from .plugins.adb_tv.events import TelevisionEvents
        from .assistant.wake_events import WakeEvents

        return ObservationsPlugin(
            service,
            {
                "wake": WakeEvents(service),
                "reyee": ReyeeEvents(config.state_dir / "observations"),
                "adb_tv": TelevisionEvents(),
            },
        )

    def notifications():
        from .plugins.notifications.plugin import NotificationsPlugin

        return NotificationsPlugin(service)

    def tasks():
        from .plugins.tasks.plugin import TasksPlugin

        return TasksPlugin(service)

    def media():
        from .plugins.media.plugin import MediaPlugin

        return MediaPlugin(service)

    def search():
        from .plugins.search.plugin import SearchPlugin

        return SearchPlugin(service)

    def models():
        from .plugins.models.plugin import ModelsPlugin

        return ModelsPlugin(service)

    def decisions():
        from .plugins.decisions.plugin import DecisionsPlugin

        return DecisionsPlugin(service)

    service.voice_io = VoiceBinding(config.input_device, config.default_output)
    service.devices = DeviceDirectory(config.devices, service.voice_io)
    from .devices.resources import DeviceResources
    from .devices.operations import DeviceOperations

    service.device_resources = DeviceResources()
    service.device_operations = DeviceOperations(service.devices, service.device_resources)
    from .devices.outputs import AudioOutputs

    service.audio_outputs = AudioOutputs(config.devices, service.device_resources)
    from .devices.inputs import AudioInputs

    service.audio_inputs = AudioInputs(config.devices, service.device_resources)
    from .devices.video import VideoInputs

    service.video_inputs = VideoInputs(config.devices)
    from .devices.cameras import CameraControls

    service.camera_controls = CameraControls(config.devices)

    def airplay():
        from .plugins.airplay.plugin import AirPlayPlugin

        return AirPlayPlugin(config, service.devices, service.audio_outputs, service.voice_io)

    def go2rtc():
        from .plugins.go2rtc.plugin import Go2RTCPlugin

        return Go2RTCPlugin(config, service.devices, service.video_inputs, service.camera_controls)

    def apple_home():
        from .plugins.apple_home.plugin import AppleHomePlugin

        return AppleHomePlugin(config, store, service.devices)

    def web():
        from .plugins.web.plugin import WebPlugin

        return WebPlugin(service)

    runtime = Runtime(store, {k: v for k, v in config.bindings.items() if v in config.plugins})
    runtime.add(WorkFilesMaintenance(service.workfiles))
    from .plugins.ai_usage.plugin import AIUsagePlugin

    usage_plugin = AIUsagePlugin(service)
    service.ai_usage = usage_plugin
    # Metering is mandatory for built-in paid routes, even in a minimal plugin list.
    if "ai_usage" not in config.plugins:
        runtime.add(usage_plugin)
    factories = {
        "pipewire": lambda: PipeWirePlugin(
            config, store, service.devices, service.audio_outputs, service.voice_io, service.audio_inputs
        ),
        "voice_disabled": VoicePlaceholder,
        "airplay": airplay,
        "go2rtc": go2rtc,
        "web": web,
        "tasks": tasks,
        "media": media,
        "search": search,
        "decisions": decisions,
        "models": models,
        "ai_usage": lambda: usage_plugin,
        "notifications": notifications,
        "events": events,
        "observations": observations,
        "integrations": integrations,
        "apple_home": apple_home,
        "pi": lambda: PiPlugin(
            config,
            store,
            service.audio_received,
            service.events.put,
            service.speak,
            service.tool_control,
            service.mcp_registry if "integrations" in config.plugins else None,
            usage_plugin.service,
            skill_paths=assistant_skill_paths(config),
        ),
        "gesture": GesturePlugin,
        "hid_buttons": lambda: HidButtonsPlugin(config),
        "memory": lambda: MemoryPlugin(
            config,
            store,
            Mem0Memory(
                config.state_dir / "mem0",
                config.models / "memory-embedding",
                lambda messages, schema: memory_complete(service, messages, schema),
                config.workspace,
            ),
            lambda: service.active or service.backend.busy,
        ),
    }
    from importlib import import_module

    for name, path in config.local_plugins.items():
        if name in factories:
            raise ValueError(f"Cannot override bundled plugin name: {name}")
        module, separator, attribute = path.partition(":")
        if not separator:
            raise ValueError("Local plugin factory must be module:attribute")
        factory = getattr(import_module(module), attribute)
        factories[name] = lambda factory=factory: factory(service)
    for name in config.plugins:
        if name not in factories:
            raise ValueError(f"Unknown trusted plugin: {name}")
        if name != "memory" or config.memory_enabled:
            plugin = factories[name]()
            if plugin.manifest.name != name:
                raise ValueError("Plugin manifest name must match its registry name")
            runtime.add(plugin)
    runtime.bindings = {k: v for k, v in runtime.bindings.items() if v in runtime.plugins}
    runtime.add(
        DevicesPlugin(
            service.devices,
            service.video_inputs,
            service.camera_controls,
            service.audio_inputs,
            service.audio_outputs,
        )
    )
    runtime.add(
        AssistantPlugin(service, tuple(config.bindings[key] for key in ("audio", "trigger", "brain")))
    )
    service.audio_plugin, service.brain_plugin = runtime.resolve("audio"), runtime.resolve("brain")
    service.route, service.player = runtime.resolve("audio").route, runtime.resolve("audio").player
    service.recognizer = runtime.resolve("trigger").recognizer
    service.backend, service.voice = runtime.resolve("brain").backend, runtime.resolve("brain").voice
    from .plugins.pi.executor import TaskExecutor

    service.task_executor = lambda notify: TaskExecutor(service.backend, notify)
    service.memory = runtime.plugins.get(config.bindings.get("memory"))
    service.backend.on_task_done = service.memory.enqueue if service.memory else None
    from .conversation.service import Conversation

    service.conversation = Conversation(service)
    runtime.add(service.conversation)
    service.interaction = None  # TODO: independent speech acceptance.
    return runtime


class AssistantPlugin:
    from .kernel.runtime import Manifest

    manifest = Manifest("assistant", ("pipewire", "voice_disabled", "pi"), critical=True)

    def __init__(self, service, dependencies):
        self.manifest = self.Manifest("assistant", dependencies, critical=True)
        self.service = service

    async def start(self, context):
        self.service.context = context
        context.register(Control, self.service.control)
        from .kernel.contracts import VoiceDecisionRequest

        context.register(VoiceDecisionRequest, self.service.voice_control.request)

        async def trigger(request):
            accepted = await self.service.wake(source=request.source)
            context.publish("assistant.triggered", source=request.source)
            return {"accepted": bool(accepted)}

        context.register(Trigger, trigger)
        context.spawn(self.service._events(), "voice-events")
        context.spawn(self.service._maintenance(), "idle-and-devices")
        if self.service.interaction:
            context.own(self.service.interaction.close)
            context.spawn(self.service.interaction.prepare(), "speech-prepare")
