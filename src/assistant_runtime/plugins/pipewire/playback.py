"""PipeWire PCM writer and optional reference mirror for an external output."""

import asyncio
from .audio import audio_env


class PipeWirePlayback:
    def __init__(self, process):
        self.process = process

    @property
    def healthy(self):
        return self.process.returncode is None

    @classmethod
    async def open(cls, sink, rate):
        process = await asyncio.create_subprocess_exec(
            "pacat",
            "--playback",
            "--raw",
            "--format=s16le",
            f"--rate={rate}",
            "--channels=1",
            "--latency-msec=120",
            "--process-time-msec=20",
            "--volume=65536",
            "--client-name=personal-agent-output",
            f"--device={sink}",
            env=audio_env(),
            stdin=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        return cls(process)

    async def write(self, pcm):
        if not self.healthy:
            raise RuntimeError("PipeWire playback process closed")
        self.process.stdin.write(pcm)
        await asyncio.wait_for(self.process.stdin.drain(), 0.2)

    async def close(self):
        if self.healthy:
            self.process.kill()
            await self.process.wait()


class MirroredPlayback:
    def __init__(self, output, reference):
        self.output, self.reference = output, reference

    @property
    def healthy(self):
        return self.output.healthy and self.reference.healthy

    async def write(self, pcm):
        await asyncio.gather(self.output.write(pcm), self.reference.write(pcm))

    async def close(self):
        await asyncio.gather(self.output.close(), self.reference.close())
