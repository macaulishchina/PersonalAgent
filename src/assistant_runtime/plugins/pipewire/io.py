"""PipeWire endpoint providers. No voice-session activation or device switching."""

import asyncio
import copy
import json

from .audio import command, audio_env, PINNED_STREAM_ARGS
from .devices import resolve
from .playback import PipeWirePlayback


class PipeWireCapture:
    def __init__(self, process):
        self.process = process

    @property
    def healthy(self):
        return self.process.returncode is None

    async def read(self, size):
        return await self.process.stdout.readexactly(size)

    async def close(self):
        if self.healthy:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), 2)
            except TimeoutError:
                self.process.kill()
                await self.process.wait()


class PipeWireEndpoint:
    def __init__(self, spec, minimum_volume=0):
        self.spec = copy.deepcopy(spec)
        self.minimum_volume = minimum_volume
        self.volume = None

    async def node(self):
        sources = json.loads(await command("pactl", "-f", "json", "list", "sources"))
        sinks = json.loads(await command("pactl", "-f", "json", "list", "sinks"))
        node = resolve(self.spec, sources, sinks)
        if not node:
            raise RuntimeError("Configured device unavailable; no fallback")
        return node

    async def open(self, rate):
        node = await self.node()
        if self.spec["kind"] == "speaker":
            return await PipeWirePlayback.open(node["name"], rate)
        process = await asyncio.create_subprocess_exec(
            "pacat",
            "--record",
            "--raw",
            "--format=s16le",
            f"--rate={rate}",
            "--channels=1",
            "--latency-msec=20",
            "--client-name=personal-agent-microphone",
            f"--device={node['name']}",
            *PINNED_STREAM_ARGS,
            env=audio_env(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        return PipeWireCapture(process)

    async def ensure_ready(self):
        await self.node()

    async def close(self):
        # All local transport processes belong to the stream registry, not this provider.
        pass

    async def set_volume(self, value):
        if type(value) is not int or not 0 <= value <= 100 or self.spec["kind"] != "speaker":
            raise ValueError("Speaker volume must be 0..100")
        node = await self.node()
        target = max(value, self.minimum_volume) if value else 0
        await command("pactl", "set-sink-mute", node["name"], "1" if target == 0 else "0")
        await command("pactl", "set-sink-volume", node["name"], f"{target}%")
        self.volume = target
        return {"volume_percent": target, "applied": True, "scope": "device"}
