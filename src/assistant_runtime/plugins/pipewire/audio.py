import asyncio
import json
import logging
import os
import time

import numpy as np
from pywebrtc_audio import AudioProcessor

log = logging.getLogger(__name__)


def audio_env():
    env = os.environ.copy()
    env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    env.setdefault("PULSE_SERVER", f"unix:{env['XDG_RUNTIME_DIR']}/pulse/native")
    return env


async def command(*args, timeout=12):
    p = await asyncio.create_subprocess_exec(
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
        raise RuntimeError(f"{args[0]} failed: {err.decode(errors='replace').strip()[:250]}")
    return out.decode().strip()


class AudioRoute:
    def __init__(self, config, store):
        self.config, self.store = config, store
        self.room = store.get("room", config.default_room)
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

    async def devices(self):
        sources = json.loads(await command("pactl", "-f", "json", "list", "short", "sources"))
        sinks = json.loads(await command("pactl", "-f", "json", "list", "short", "sinks"))
        return sources, sinks

    async def ensure(self, force=False, check_volume=False):
        async with self.lock:
            sources, sinks = await self.devices()
            target = self.config.speakers[self.room]
            bluetooth = bool(__import__("re").fullmatch(r"(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}", target))
            mac = target.replace(":", "_")
            physical_source = next(
                (
                    x["name"]
                    for x in sources
                    if self.config.source_match in x["name"] and not x["name"].endswith(".monitor")
                ),
                None,
            )
            physical_sink = next(
                (
                    x["name"]
                    for x in sinks
                    if (f"bluez_output.{mac}." in x["name"] if bluetooth else target == x["name"])
                ),
                None,
            )
            if not physical_source or not physical_sink:
                self.ready.clear()
                if self.module:
                    await self._unload()
                self.source = physical_source
                self.sink = self.physical_sink = physical_sink
                self.last_error = (
                    "microphone unavailable" if not physical_source else f"{self.room} speaker unavailable"
                )
                if bluetooth and not physical_sink and time.monotonic() - self.last_connect > 20:
                    self.last_connect = time.monotonic()
                    try:
                        await command("bluetoothctl", "connect", self.config.speakers[self.room], timeout=8)
                    except (RuntimeError, TimeoutError):
                        pass
                raise RuntimeError(self.last_error)
            if self.ready.is_set() and not force and physical_sink == self.physical_sink:
                if self.source in {s["name"] for s in sources} and self.sink in {s["name"] for s in sinks}:
                    if check_volume:
                        await self.ensure_minimum_volume()
                    return
            self.ready.clear()
            await self._unload()
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
            log.info("audio_ready room=%s echo_cancel=%s", self.room, bool(self.module))

    async def ensure_minimum_volume(self):
        self.volume = None
        if not self.physical_sink:
            return
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
        if self.physical_sink:
            await command("pactl", "set-sink-mute", self.physical_sink, "1" if value == 0 else "0")
            await command("pactl", "set-sink-volume", self.physical_sink, f"{value}%")
            if value:
                await self.ensure_minimum_volume()

    async def select(self, room):
        if room not in self.config.speakers:
            raise ValueError("unknown speaker")
        self.room = room
        self.store.set("room", room)
        self.ready.clear()
        await self.ensure(force=True)

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
        for p in (self.capture, self.reference_capture):
            if p and p.returncode is None:
                p.terminate()
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
        await self._unload()

    async def _record(self, source, name):
        return await asyncio.create_subprocess_exec(
            "pacat",
            "--record",
            "--raw",
            "--format=s16le",
            "--rate=16000",
            "--channels=1",
            "--latency-msec=20",
            f"--client-name={name}",
            f"--device={source}",
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
                capture = self.capture = await self._record(self.source, "personal-agent-microphone")
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
                            asyncio.gather(
                                capture.stdout.readexactly(640), reference.stdout.readexactly(640)
                            ),
                            3,
                        )
                        data = processor.process(
                            np.frombuffer(near, dtype="<i2").copy(), np.frombuffer(far, dtype="<i2").copy()
                        ).tobytes()
                    else:
                        near = data = await asyncio.wait_for(capture.stdout.readexactly(640), 3)
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
                for p in (capture, reference):
                    if p and p.returncode is None:
                        p.terminate()
                        await p.wait()
                self.raw_frame = None
