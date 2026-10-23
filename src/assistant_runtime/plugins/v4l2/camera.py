"""On-demand V4L2 acquisition through upstream FFmpeg, with no standby capture."""

import asyncio
import io
import os
from pathlib import Path
import stat
import time

from imageio_ffmpeg import get_ffmpeg_exe
from PIL import Image

from assistant_runtime.api.contracts import VideoFrame


def validate_connection(spec):
    values = spec["connection"]
    allowed = {"path", "width", "height", "framerate", "input_format", "timeout_seconds"}
    if set(values) - allowed:
        raise ValueError("Unknown V4L2 connection settings")
    path = values.get("path")
    if (
        not isinstance(path, str)
        or Path(path).parent not in (Path("/dev/v4l/by-id"), Path("/dev/v4l/by-path"))
        or ".." in Path(path).parts
        or not Path(path).name.endswith("-video-index0")
    ):
        raise ValueError("V4L2 requires an exact stable /dev/v4l/by-id or by-path video-index0 path")
    for name, default, maximum in (("width", 1280, 1920), ("height", 720, 1080), ("framerate", 15, 30)):
        value = values.get(name, default)
        if type(value) is not int or not 1 <= value <= maximum:
            raise ValueError(f"Invalid V4L2 {name}")
    if values.get("input_format", "mjpeg") not in {"mjpeg", "yuyv422"}:
        raise ValueError("Unsupported V4L2 input format")
    timeout = values.get("timeout_seconds", 8)
    if type(timeout) not in (int, float) or not 0 < timeout <= 10:
        raise ValueError("Invalid V4L2 capture timeout")


def validate_jpeg(data, expected):
    with Image.open(io.BytesIO(data)) as image:
        if image.format != "JPEG" or image.size != expected:
            raise ValueError("Camera returned an unexpected image format or dimensions")
        image.load()


class V4L2Camera:
    def __init__(self, device_id, spec):
        validate_connection(spec)
        self.device_id = device_id
        self.options = spec["connection"]
        self.path = Path(self.options["path"])
        self.width = self.options.get("width", 1280)
        self.height = self.options.get("height", 720)
        self.generation = 1
        self.identity = None
        self.lock = asyncio.Lock()
        self.processes = set()
        self.closed = False
        self.last_error = None
        self.last_capture = None
        self.capture_count = 0
        self.formats_checked = False

    def node_identity(self):
        node = self.path.stat()
        if not stat.S_ISCHR(node.st_mode):
            raise ValueError("Configured camera is not a video character device")
        if not os.access(self.path, os.R_OK | os.W_OK):
            raise PermissionError("Camera access denied; use the host's normal video-device permissions")
        return node.st_dev, node.st_ino, node.st_rdev

    async def execute(self, arguments, limit):
        if self.closed:
            raise RuntimeError("Camera provider closed")
        binary = await asyncio.to_thread(get_ffmpeg_exe)
        if self.closed:
            raise RuntimeError("Camera provider closed")
        process = await asyncio.create_subprocess_exec(
            binary,
            "-hide_banner",
            "-nostdin",
            *arguments,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self.processes.add(process)

        async def bounded(reader, maximum):
            data = bytearray()
            while block := await reader.read(min(65536, maximum + 1 - len(data))):
                data.extend(block)
                if len(data) > maximum:
                    raise ValueError("Camera output exceeded its size limit")
            return bytes(data)

        readers = [
            asyncio.create_task(bounded(process.stdout, limit)),
            asyncio.create_task(bounded(process.stderr, 32768)),
        ]
        try:
            async with asyncio.timeout(self.options.get("timeout_seconds", 8)):
                out, err = await asyncio.gather(*readers)
                await process.wait()
            if self.closed:
                raise RuntimeError("Camera provider closed during acquisition")
            return process.returncode, out, err.decode(errors="replace")
        finally:
            if process.returncode is None:
                process.kill()
            for task in readers:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*readers, return_exceptions=True)
            await process.wait()
            self.processes.discard(process)

    async def inspect(self):
        try:
            identity = self.node_identity()
        except (OSError, ValueError):
            if self.identity is not None:
                self.generation += 1
                self.identity = None
            self.formats_checked = False
            raise
        if identity != self.identity:
            self.identity = identity
            self.generation += 1
            self.formats_checked = False
            self.last_error = None
        if not self.formats_checked and not self.lock.locked():
            async with self.lock:
                if not self.formats_checked:
                    # Enumerating formats opens the node but never starts frame capture.
                    _, _, description = await self.execute(
                        ["-f", "v4l2", "-list_formats", "all", "-i", str(self.path)], 0
                    )
                    required = self.options.get("input_format", "mjpeg")
                    if not any(
                        required in line and f"{self.width}x{self.height}" in line
                        for line in description.splitlines()
                    ):
                        raise RuntimeError("Requested camera format or resolution not supported")
                    self.formats_checked = True
        return {
            "capturing": self.lock.locked(),
            "capture_mode": "on_request",
            "width": self.width,
            "height": self.height,
            "input_format": self.options.get("input_format", "mjpeg"),
            "formats_checked": self.formats_checked,
            "capture_count": self.capture_count,
            "last_capture_at": self.last_capture,
            "raw_video_saved": False,
        }

    async def capture(self):
        if self.closed:
            raise RuntimeError("Camera provider closed")
        if self.lock.locked() or self.processes:
            raise RuntimeError("Camera busy; another acquisition is in progress")
        async with self.lock:
            try:
                identity = self.node_identity()
                if identity != self.identity:
                    self.identity = identity
                    self.generation += 1
                    self.formats_checked = False
                epoch = self.generation
                options = [
                    "-loglevel",
                    "error",
                    "-f",
                    "v4l2",
                    "-input_format",
                    self.options.get("input_format", "mjpeg"),
                    "-video_size",
                    f"{self.width}x{self.height}",
                    "-framerate",
                    str(self.options.get("framerate", 15)),
                    "-i",
                    str(self.path),
                    "-frames:v",
                    "1",
                    "-an",
                    "-c:v",
                    "copy" if self.options.get("input_format", "mjpeg") == "mjpeg" else "mjpeg",
                ]
                if self.options.get("input_format", "mjpeg") != "mjpeg":
                    options.extend(["-threads", "1", "-q:v", "5"])
                code, data, error = await self.execute([*options, "-f", "image2pipe", "pipe:1"], 1024 * 1024)
                if code:
                    raise RuntimeError("Camera acquisition failed: " + error.strip()[:200])
                await asyncio.to_thread(validate_jpeg, data, (self.width, self.height))
                if identity != self.node_identity() or epoch != self.generation or self.closed:
                    raise RuntimeError("Camera identity or generation changed; frame discarded")
                received_at = time.time()
                frame = VideoFrame(self.device_id, data, self.width, self.height, received_at, epoch)
                self.capture_count += 1
                self.last_capture = received_at
                self.last_error = None
                return frame
            except BaseException as exc:
                self.last_error = (
                    "Capture interrupted" if isinstance(exc, asyncio.CancelledError) else str(exc)[:200]
                )
                raise

    async def close(self):
        self.closed = True
        self.generation += 1
        processes = list(self.processes)
        for process in processes:
            if process.returncode is None:
                process.kill()
        await asyncio.gather(*(process.wait() for process in processes), return_exceptions=True)
