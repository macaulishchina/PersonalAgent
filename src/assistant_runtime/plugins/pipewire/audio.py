from assistant_runtime.api.system import system
import asyncio
import copy
import json
import logging
import os
import time

from assistant_runtime.api.contracts import VoiceBinding
from .devices import resolve, validate_device
from .volume import apply_hardware_volume

import numpy as np
from pywebrtc_audio import AudioProcessor

log = logging.getLogger(__name__)

PINNED_STREAM_ARGS = (
    "--property=node.dont-reconnect=true",
    "--property=node.dont-fallback=true",
    "--property=node.dont-move=true",
)


def audio_env():
    env = os.environ.copy()
    env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    env.setdefault("PULSE_SERVER", f"unix:{env['XDG_RUNTIME_DIR']}/pulse/native")
    return env


async def command(*args, timeout=12):
    p = await system.spawn(
        *args, env=audio_env(), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        out, err = await asyncio.wait_for(p.communicate(), timeout)
    except BaseException:
        if p.returncode is None:
            p.kill()
            await p.wait()
        raise
    if p.returncode:
        raise RuntimeError(f"{args[0]} failed: {(err or out).decode(errors='replace').strip()[:250]}")
    return out.decode().strip()


class AudioRoute:
    def __init__(self, config, store, outputs=None, voice_io=None, inputs=None):
        self.config, self.store = config, store
        self._voice_io = voice_io or VoiceBinding(config.input_device, config.default_output)
        self._input_spec = copy.deepcopy(config.devices.get(self.input_device, {}))
        self._output_spec = copy.deepcopy(config.devices.get(self.output_device, {}))
        for spec in config.devices.values():
            if spec["adapter"] == "pipewire" and spec.get("enabled", True):
                validate_device(spec)
        if self.output_device and self.output_device not in config.output_devices:
            raise ValueError("Configured voice output is unavailable or disabled")
        if self.input_device and self._input_spec.get("adapter") != "pipewire":
            raise ValueError("Selected microphone requires its configured adapter")
        self.outputs, self.inputs = outputs, inputs
        self.external_output = None
        # Observe the device, never restore an old service volume over its setting.
        self.volume = None
        self.source = self.sink = None
        self.physical_sink = None
        self.module = None
        self.bridge = None
        self.reference_capture = None
        self.raw_frame = None
        self.capture = None
        self.lock = asyncio.Lock()
        self.ready = asyncio.Event()
        self.last_error = None
        self.last_connect = 0

    @property
    def input_device(self):
        return self._voice_io.input_device

    @property
    def output_device(self):
        return self._voice_io.output_device

    @property
    def hardware_volume_configured(self):
        return (
            self._output_spec.get("adapter") == "pipewire"
            and "hardware_volume" in self._output_spec["connection"]
        )

    async def devices(self):
        sources = json.loads(await command("pactl", "-f", "json", "list", "sources"))
        sinks = json.loads(await command("pactl", "-f", "json", "list", "sinks"))
        return sources, sinks

    async def ensure(self, force=False, check_volume=False):
        if not self.input_device or not self.output_device:
            raise RuntimeError("Main audio bindings are not configured")
        async with self.lock:
            sources, sinks = await self.devices()
            spec = self._output_spec
            external = self.outputs.get(self.output_device) if spec["adapter"] != "pipewire" else None
            if external:
                try:
                    await external.ensure_ready()
                except (RuntimeError, TimeoutError, OSError):
                    self.ready.clear()
                    self.last_error = f"{self.output_device} output unavailable"
                    raise
            target = spec["connection"].get("address") if not external else None
            bluetooth = target is not None
            source_node = resolve(self._input_spec, sources, sinks)
            sink_node = resolve(spec, sources, sinks) if not external else None
            physical_source = source_node["name"] if source_node else None
            physical_sink = sink_node["name"] if sink_node else None
            if not physical_source or (not physical_sink and not external):
                self.ready.clear()
                if self.module:
                    await self._unload()
                self.source = physical_source
                self.sink = self.physical_sink = physical_sink
                self.last_error = (
                    "microphone unavailable"
                    if not physical_source
                    else f"{self.output_device} speaker unavailable"
                )
                if bluetooth and not physical_sink and time.monotonic() - self.last_connect > 20:
                    self.last_connect = time.monotonic()
                    try:
                        await command("bluetoothctl", "connect", target, timeout=8)
                    except (RuntimeError, TimeoutError):
                        pass
                raise RuntimeError(self.last_error)
            if (
                self.ready.is_set()
                and not force
                and external is self.external_output
                and physical_sink == self.physical_sink
            ):
                if (
                    external
                    and self.source in {s["name"] for s in sources}
                    and (not self.module or self.sink in {s["name"] for s in sinks})
                ):
                    if check_volume:
                        await self.ensure_minimum_volume()
                    return
                if self.source in {s["name"] for s in sources} and self.sink in {s["name"] for s in sinks}:
                    if check_volume:
                        await self.ensure_minimum_volume()
                    return
            self.ready.clear()
            await self._unload()
            self.external_output = external
            self.physical_sink = physical_sink
            self.source, self.sink = physical_source, physical_sink
            if self.config.echo_cancel:
                # A private playback bus supplies only the assistant's sound to
                # AEC3. Other applications are not treated as our echo reference.
                await self._clean_stale_modules()
                self.module = await command(
                    "pactl",
                    "load-module",
                    "module-null-sink",
                    "sink_name=personal_agent_echo_output",
                    "rate=48000",
                    "channels=1",
                    "sink_properties=device.description=PersonalAgent_Playback",
                )
                try:
                    if physical_sink:
                        self.bridge = await command(
                            "pactl",
                            "load-module",
                            "module-loopback",
                            "source=personal_agent_echo_output.monitor",
                            f"sink={physical_sink}",
                            "latency_msec=30",
                            "source_dont_move=true",
                            "sink_dont_move=true",
                        )
                except BaseException:
                    await self._unload()
                    raise
                self.sink = "personal_agent_echo_output"
                await asyncio.sleep(0.3)
            await self.ensure_minimum_volume()
            self.ready.set()
            self.last_error = None
            log.info("audio_ready output_device=%s echo_cancel=%s", self.output_device, bool(self.module))

    async def ensure_minimum_volume(self):
        self.volume = None
        if self.external_output:
            known = self.external_output.volume
            if known is not None and 0 < known < self.config.minimum_volume:
                await self.external_output.set_volume(self.config.minimum_volume)
            self.volume = self.external_output.volume
            return
        if not self.physical_sink:
            return
        # The main voice output can use a private AEC sink, bypassing the
        # physical provider's open(). Initialize the resolved hardware here too.
        if self.hardware_volume_configured:
            nodes = json.loads(await command("pactl", "-f", "json", "list", "sinks"))
            node = next((s for s in nodes if s["name"] == self.physical_sink), None)
            await apply_hardware_volume(self._output_spec, node)
        try:
            sinks = json.loads(await command("pactl", "-f", "json", "list", "sinks"))
            sink = next((s for s in sinks if s["name"] == self.physical_sink), None)
            channels = list((sink or {}).get("volume", {}).values())
            values = [c["value"] for c in channels]
            if not values or any(type(v) is not int or v < 0 for v in values):
                return
            floor = round(self.config.minimum_volume * 65536 / 100)
            adjusted = [max(v, floor) for v in values]
            if adjusted != values:
                # Per-channel values preserve a louder channel and existing balance
                # above the floor. No automatic unmute or virtual-sink gain change.
                await command("pactl", "set-sink-volume", self.physical_sink, *map(str, adjusted))
            self.volume = round(sum(adjusted) / len(adjusted) * 100 / 65536)
        except (RuntimeError, TimeoutError, OSError, ValueError, TypeError, KeyError, AttributeError):
            log.info("speaker_volume_unavailable: leaving device volume unchanged")

    async def set_volume(self, value):
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 100:
            raise ValueError("volume must be an integer from 0 to 100")
        self.volume = value
        self.store.set("volume", value)
        if self.external_output:
            await self.external_output.set_volume(max(value, self.config.minimum_volume) if value else 0)
            self.volume = self.external_output.volume
        elif self.physical_sink:
            await command("pactl", "set-sink-mute", self.physical_sink, "1" if value == 0 else "0")
            await command("pactl", "set-sink-volume", self.physical_sink, f"{value}%")
            if value:
                await self.ensure_minimum_volume()

    async def open_output(self, rate):
        from .playback import PipeWirePlayback, MirroredPlayback

        if self.external_output:
            transport = await self.outputs.open(self.output_device, rate, owner="voice")
            if self.module:
                try:
                    reference = await PipeWirePlayback.open(self.sink, rate)
                    return MirroredPlayback(transport, reference)
                except BaseException:
                    await transport.close()
                    raise
            return transport
        return await self.outputs.open(
            self.output_device, rate, owner="voice", opener=lambda: PipeWirePlayback.open(self.sink, rate)
        )

    async def _clean_stale_modules(self):
        # This pactl build omits module IDs in JSON; the short table has them.
        modules = await command("pactl", "list", "short", "modules")
        for line in reversed(modules.splitlines()):
            fields = line.split("\t")
            if (
                len(fields) >= 3
                and fields[0].isdigit()
                and fields[1] in {"module-echo-cancel", "module-null-sink", "module-loopback"}
                and "personal_agent_echo_" in fields[2]
            ):
                await command("pactl", "unload-module", fields[0])

    async def _unload(self):
        if self.capture:
            await self.capture.close()
        if self.reference_capture and self.reference_capture.returncode is None:
            self.reference_capture.terminate()
        for name in ("bridge", "module"):
            module = getattr(self, name)
            if module:
                try:
                    await command("pactl", "unload-module", module)
                except RuntimeError:
                    pass
                setattr(self, name, None)

    async def close(self):
        self.ready.clear()
        # Stream ownership belongs to the player/registry, never close a provider globally here.
        await self._unload()

    async def _record(self, source, name):
        return await system.spawn(
            "pacat",
            "--record",
            "--raw",
            "--format=s16le",
            "--rate=16000",
            "--channels=1",
            "--latency-msec=20",
            f"--client-name={name}",
            f"--device={source}",
            *PINNED_STREAM_ARGS,
            env=audio_env(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )

    async def frames(self):
        while True:
            capture = reference = None
            try:
                try:
                    await self.ensure()
                except RuntimeError:
                    # Listen without redirecting to another room while offline.
                    if not self.source:
                        raise
                if self.inputs is None:
                    raise RuntimeError("Audio input providers not configured")
                capture = self.capture = await self.inputs.open(self.input_device, 16000, owner="voice")
                processor = None
                if self.module:
                    reference = self.reference_capture = await self._record(
                        self.sink + ".monitor", "personal-agent-echo-reference"
                    )
                    processor = AudioProcessor(
                        sample_rate=16000,
                        echo_cancellation=True,
                        noise_suppression=True,
                        auto_gain_control=False,
                    )
                while True:
                    # Equal clocked 20 ms blocks: AEC3 estimates acoustic delay.
                    # Keep unprocessed speech for local keyword detection only.
                    if reference:
                        near, far = await asyncio.wait_for(
                            asyncio.gather(capture.read(640), reference.stdout.readexactly(640)),
                            3,
                        )
                        data = processor.process(
                            np.frombuffer(near, dtype="<i2").copy(), np.frombuffer(far, dtype="<i2").copy()
                        ).tobytes()
                    else:
                        near = data = await asyncio.wait_for(capture.read(640), 3)
                    self.raw_frame = near
                    yield data
            except asyncio.CancelledError:
                raise
            except (RuntimeError, asyncio.IncompleteReadError, TimeoutError, OSError) as exc:
                self.ready.clear()
                self.last_error = str(exc)[:200]
                log.warning("audio_input_waiting: %s", self.last_error)
                await asyncio.sleep(1)
            finally:
                if capture:
                    await capture.close()
                if reference and reference.returncode is None:
                    reference.terminate()
                    await reference.wait()
                self.capture = self.reference_capture = None
                self.raw_frame = None
