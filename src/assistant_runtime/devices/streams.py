"""Adapter-independent PCM stream ownership; closing one stream releases only its lease."""

import asyncio

from .resources import DeviceResources


class DeviceStream:
    def __init__(self, registry, transport, lease, sample_rate):
        self.registry, self.transport, self.lease = registry, transport, lease
        self.sample_rate = sample_rate
        self.closed = False
        self.close_task = None

    @property
    def healthy(self):
        return not self.closed and self.close_task is None and self.transport.healthy

    async def read(self, size):
        if self.registry.kind != "microphone" or type(size) is not int or not 0 < size <= 96000 or size % 2:
            raise ValueError("Capture reads require a bounded whole PCM16 sample count")
        if not self.healthy:
            raise RuntimeError("Device capture stream is closed")
        data = await self.transport.read(size)
        if not isinstance(data, bytes) or len(data) != size:
            raise RuntimeError("Capture provider returned an invalid PCM block")
        return data

    async def write(self, pcm):
        if (
            self.registry.kind != "speaker"
            or not isinstance(pcm, bytes)
            or len(pcm) != self.sample_rate // 50 * 2
        ):
            raise ValueError("Playback writes require exactly 20ms mono PCM16")
        if not self.healthy:
            raise RuntimeError("Device playback stream is closed")
        await self.transport.write(pcm)

    async def close(self):
        if self.closed:
            return

        async def cleanup():
            await self.transport.close()
            self.closed = True
            self.lease.release()
            self.registry.streams.discard(self)

        if self.close_task is None or (self.close_task.done() and not self.closed):
            self.close_task = asyncio.create_task(cleanup(), name="device-stream-cleanup")
            self.registry.cleanups.add(self.close_task)

            def done(task):
                self.registry.cleanups.discard(task)
                if not task.cancelled():
                    task.exception()  # Observed even if the caller disconnected during cleanup.

            self.close_task.add_done_callback(done)
        await asyncio.shield(self.close_task)


class AudioStreams:
    kind = None
    resource = None

    def __init__(self, devices, resources=None):
        self.devices = devices
        self.resources = resources or DeviceResources()
        self.providers = {}
        self.sharing = {}
        self.streams = set()
        self.opening = set()
        self.cleanups = set()
        self.closed = False

    def register(self, owner, device_id, provider, *, shared=False):
        spec = self.devices[device_id]
        if spec["adapter"] != owner or spec["kind"] != self.kind or not spec.get("enabled", True):
            raise ValueError("Audio stream owner/role mismatch")
        if device_id in self.providers:
            raise ValueError("Duplicate audio stream provider")
        if type(shared) is not bool:
            raise ValueError("Sharing policy must be boolean")
        self.providers[device_id] = provider
        self.sharing[device_id] = shared

    def get(self, device_id):
        if device_id not in self.providers:
            raise ValueError(f"No media {self.kind} provider registered: {device_id}")
        return self.providers[device_id]

    async def open(self, device_id, sample_rate, *, owner, opener=None):
        if self.closed:
            raise RuntimeError("Device streams are stopping")
        if type(sample_rate) is not int or not 8000 <= sample_rate <= 48000 or sample_rate % 50:
            raise ValueError("PCM sample rate must be 8000..48000")
        provider = self.get(device_id)
        lease = self.resources.acquire(device_id, self.resource, owner, shared=self.sharing[device_id])
        current = asyncio.current_task()
        self.opening.add(current)
        stream = None
        try:
            transport = await (opener() if opener else provider.open(sample_rate))
            stream = DeviceStream(self, transport, lease, sample_rate)
            self.streams.add(stream)
            if self.closed:
                await stream.close()
                raise RuntimeError("Device streams stopped during open")
            return stream
        except BaseException:
            if stream is None:
                lease.release()
            raise
        finally:
            self.opening.discard(current)

    async def close(self):
        self.closed = True
        opening = list(self.opening - {asyncio.current_task()})
        for task in opening:
            if not task.cancelling():
                task.cancel()
        await asyncio.gather(*opening, return_exceptions=True)
        results = await asyncio.gather(
            *(stream.close() for stream in list(self.streams)), return_exceptions=True
        )
        await asyncio.gather(*list(self.cleanups), return_exceptions=True)
        errors = [r for r in results if isinstance(r, BaseException)]
        if errors:
            raise RuntimeError("Device stream cleanup failed; leases retained") from errors[0]
