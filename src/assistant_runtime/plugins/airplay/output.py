"""Bounded PCM -> FFmpeg MP3 -> pyatv public StreamReader streaming API."""

import asyncio
import copy
import ipaddress
import time

import pyatv
from imageio_ffmpeg import get_ffmpeg_exe
from pyatv.const import Protocol
from pyatv.interface import MediaMetadata
from pyatv.settings import AirPlayVersion
from pyatv.storage.memory_storage import MemoryStorage


def validate_connection(connection):
    required = {"host", "identifier", "timing_port", "control_port"}
    if not required <= connection.keys() or set(connection) - required - {"credentials", "password"}:
        raise ValueError("AirPlay requires host, identifier, timing_port and control_port")
    ipaddress.ip_address(connection["host"])
    if not isinstance(connection["identifier"], str) or not connection["identifier"]:
        raise ValueError("AirPlay stable identifier is required")
    ports = [connection[k] for k in ("timing_port", "control_port")]
    if any(type(p) is not int or not 1024 <= p <= 65535 for p in ports) or ports[0] == ports[1]:
        raise ValueError("AirPlay UDP ports must be distinct non-privileged ports")


class AirPlayOutput:
    def __init__(self, spec):
        validate_connection(spec["connection"])
        self.spec = copy.deepcopy(spec)
        self.config = None
        self.last_scan = 0
        self.scan_lock = asyncio.Lock()
        self.open_lock = asyncio.Lock()
        self.connection = None
        self.last_error = None
        self.volume = None  # RAOP getters may be cached/defaults, not receiver reads.
        self.opened_at = None

    async def discover(self, force=False):
        async with self.scan_lock:
            if not force and self.config and time.monotonic() - self.last_scan < 15:
                return self.config
            c = self.spec["connection"]
            matches = await pyatv.scan(
                asyncio.get_running_loop(), hosts=[c["host"]], protocol={Protocol.RAOP}, timeout=3
            )
            self.last_scan = time.monotonic()
            self.config = next((x for x in matches if x.identifier == c["identifier"]), None)
            if self.config is None:
                raise RuntimeError("Configured AirPlay identity not discovered at its host")
            return self.config

    async def ensure_ready(self):
        await self.discover()

    async def open(self, rate):
        async with self.open_lock:
            if self.connection and self.connection.healthy:
                raise RuntimeError("AirPlay device busy; existing stream preserved")
            return await self._open(rate)

    async def _open(self, rate):
        await self.close()
        cfg = await self.discover()
        c = self.spec["connection"]
        service = cfg.get_service(Protocol.RAOP)
        if c.get("credentials"):
            service.credentials = c["credentials"]
        if c.get("password"):
            service.password = c["password"]
        storage = MemoryStorage()
        settings = await storage.get_settings(cfg)
        settings.protocols.raop.protocol_version = AirPlayVersion.V2
        settings.protocols.raop.timing_port = c["timing_port"]
        settings.protocols.raop.control_port = c["control_port"]
        atv = await asyncio.wait_for(
            pyatv.connect(cfg, asyncio.get_running_loop(), protocol=Protocol.RAOP, storage=storage), 10
        )
        try:
            if self.volume is not None:
                await atv.audio.set_volume(self.volume)
            connection = await AirPlayPlayback.open(atv, rate, self)
            self.connection = connection
            self.opened_at = time.monotonic()
            self.last_error = None
            return connection
        except BaseException:
            await close_atv(atv)
            raise

    async def set_volume(self, value):
        if type(value) is not int or not 0 <= value <= 100:
            raise ValueError("Volume must be 0..100")
        if self.connection and self.connection.healthy:
            await self.connection.atv.audio.set_volume(value)
        self.volume = value
        applied = bool(self.connection and self.connection.healthy)
        return {"volume_percent": value, "applied": applied, "scope": "device" if applied else "next_stream"}

    async def close(self):
        if self.connection:
            await self.connection.close()
            self.connection = None


async def close_atv(atv):
    tasks = atv.close()
    if tasks:
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 3)


class AirPlayPlayback:
    def __init__(self, atv, process, rate, owner):
        self.atv, self.process, self.rate, self.owner = atv, process, rate, owner
        self.queue = asyncio.Queue(maxsize=75)
        self.closed = False
        self.cleanup_complete = False
        self.close_lock = asyncio.Lock()
        self.sent_bytes = 0
        self.started = time.monotonic()
        self.encoder_task = asyncio.create_task(self._encode(), name="airplay:encode")
        self.stream_task = asyncio.create_task(self._stream(), name="airplay:stream")

    @classmethod
    async def open(cls, atv, rate, owner):
        executable = await asyncio.to_thread(get_ffmpeg_exe)
        process = await asyncio.create_subprocess_exec(
            executable,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "s16le",
            "-ar",
            str(rate),
            "-ac",
            "1",
            "-i",
            "pipe:0",
            "-codec:a",
            "libmp3lame",
            "-b:a",
            "128k",
            "-f",
            "mp3",
            "-flush_packets",
            "1",
            "pipe:1",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            limit=65536,
        )
        return cls(atv, process, rate, owner)

    @property
    def healthy(self):
        return (
            not self.closed
            and self.process.returncode is None
            and not self.encoder_task.done()
            and not self.stream_task.done()
        )

    async def write(self, pcm):
        if not self.healthy:
            raise RuntimeError(self.owner.last_error or "AirPlay stream closed")
        if len(pcm) != self.rate // 50 * 2:
            raise ValueError("AirPlay expects clocked 20ms mono PCM16 frames")
        try:
            self.queue.put_nowait(pcm)
        except asyncio.QueueFull:
            raise RuntimeError("AirPlay encoder queue exceeded 1.5 seconds") from None

    async def _encode(self):
        deadline = time.monotonic()
        silence = bytes(self.rate // 50 * 2)
        try:
            while True:
                await asyncio.sleep(max(0, deadline - time.monotonic()))
                queued = not self.queue.empty()
                pcm = self.queue.get_nowait() if queued else silence
                if queued:
                    self.queue.task_done()
                self.process.stdin.write(pcm)
                await asyncio.wait_for(self.process.stdin.drain(), 0.2)
                self.sent_bytes += len(pcm)
                deadline = max(deadline + 0.02, time.monotonic())
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.owner.last_error = "AirPlay encoder failed: " + type(exc).__name__
            raise

    async def _stream(self):
        try:
            await self.atv.stream.stream_file(
                self.process.stdout, metadata=MediaMetadata(title="Personal Agent")
            )
            if not self.closed:
                self.owner.last_error = "AirPlay stream ended unexpectedly"
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.owner.last_error = "AirPlay stream failed: " + type(exc).__name__
            raise

    async def close(self):
        async with self.close_lock:
            if self.cleanup_complete:
                return
            self.closed = True
            self.encoder_task.cancel()
            # EOF releases upstream decoder executor reads before its task is cancelled.
            if self.process.returncode is None:
                self.process.kill()
                await self.process.wait()
            self.process.stdout.feed_eof()
            self.stream_task.cancel()
            await asyncio.gather(self.encoder_task, self.stream_task, return_exceptions=True)
            while not self.queue.empty():
                self.queue.get_nowait()
                self.queue.task_done()
            await close_atv(self.atv)
            self.cleanup_complete = True
