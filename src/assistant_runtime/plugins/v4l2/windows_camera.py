"""Bounded FFmpeg DirectShow acquisition; no numeric/default camera selection."""

import asyncio
import copy
import io
import re
import time

from assistant_runtime.api.contracts import VideoFrame
from assistant_runtime.api.system import system


def validate(spec):
    if spec["kind"] != "camera":
        raise ValueError("DirectShow only supports cameras")
    values = spec["connection"]
    if set(values) - {"device_path", "width", "height", "framerate", "input_format", "timeout_seconds"}:
        raise ValueError("Unknown DirectShow connection setting")
    path = values.get("device_path")
    if (
        not isinstance(path, str)
        or not path.startswith("@device_pnp_")
        or not 20 <= len(path) <= 1024
        or any(ord(c) < 32 or c == '"' for c in path)
    ):
        raise ValueError("DirectShow requires an exact alternative @device_pnp_ device path")
    for name, default, maximum in (("width", 1280, 1920), ("height", 720, 1080), ("framerate", 15, 30)):
        value = values.get(name, default)
        if type(value) is not int or not 1 <= value <= maximum:
            raise ValueError(f"Invalid DirectShow {name}")
    if values.get("input_format", "mjpeg") not in {"mjpeg", "yuyv422"}:
        raise ValueError("Unsupported DirectShow input format")
    timeout = values.get("timeout_seconds", 8)
    if type(timeout) not in (int, float) or not 0 < timeout <= 10:
        raise ValueError("Invalid DirectShow timeout")


def devices(description):
    result, video = [], False
    for line in description.splitlines():
        if re.search(r'".*" \((video|audio)\)', line):
            video = "(video)" in line
        match = re.search(r'Alternative name "(@device_pnp_[^"]+)"', line)
        if video and match:
            result.append(match[1])
            video = False
    return result


class Camera:
    def __init__(self, ident, spec):
        validate(spec)
        self.ident, self.options = ident, copy.deepcopy(spec["connection"])
        self.width, self.height = self.options.get("width", 1280), self.options.get("height", 720)
        self.generation, self.present = 1, False
        self.lock = asyncio.Lock()
        self.processes = set()
        self.closed = False
        self.capture_count, self.last_capture = 0, None

    async def execute(self, arguments, limit):
        from imageio_ffmpeg import get_ffmpeg_exe

        if self.closed:
            raise RuntimeError("Camera provider closed")
        binary = await asyncio.to_thread(get_ffmpeg_exe)
        if self.closed:
            raise RuntimeError("Camera provider closed")
        process = await system.spawn(
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
            asyncio.create_task(bounded(process.stderr, 65536)),
        ]
        try:
            async with asyncio.timeout(self.options.get("timeout_seconds", 8)):
                out, err = await asyncio.gather(*readers)
                await process.wait()
            if self.closed:
                raise RuntimeError("Camera closed during acquisition")
            return process.returncode, out, err.decode("utf-8", errors="replace")
        finally:
            if process.returncode is None:
                process.kill()
            for task in readers:
                task.cancel()
            await asyncio.gather(*readers, return_exceptions=True)
            await process.wait()
            self.processes.discard(process)

    async def inspect(self):
        _, _, description = await self.execute(["-list_devices", "true", "-f", "dshow", "-i", "dummy"], 0)
        matches = [
            path for path in devices(description) if path.casefold() == self.options["device_path"].casefold()
        ]
        present = len(matches) == 1
        if present != self.present:
            self.present = present
            self.generation += 1
        if not present:
            raise OSError("Configured DirectShow camera unavailable or ambiguous; no fallback")
        return {
            "capture_mode": "on_request",
            "capturing": self.lock.locked(),
            "width": self.width,
            "height": self.height,
            "capture_count": self.capture_count,
            "last_capture_at": self.last_capture,
            "raw_video_saved": False,
        }

    async def capture(self):
        if self.closed or self.lock.locked() or self.processes:
            raise RuntimeError("Camera closed or busy")
        async with self.lock:
            await self.inspect()
            epoch = self.generation
            format_args = (
                ["-vcodec", "mjpeg"]
                if self.options.get("input_format", "mjpeg") == "mjpeg"
                else ["-pixel_format", "yuyv422"]
            )
            code, data, _ = await self.execute(
                [
                    "-loglevel",
                    "error",
                    "-f",
                    "dshow",
                    *format_args,
                    "-video_size",
                    f"{self.width}x{self.height}",
                    "-framerate",
                    str(self.options.get("framerate", 15)),
                    "-i",
                    "video=" + self.options["device_path"],
                    "-frames:v",
                    "1",
                    "-an",
                    "-c:v",
                    "mjpeg",
                    "-threads",
                    "1",
                    "-q:v",
                    "5",
                    "-f",
                    "image2pipe",
                    "pipe:1",
                ],
                1024 * 1024,
            )
            if code:
                raise OSError("DirectShow acquisition failed; check device permissions, format and ownership")
            from PIL import Image

            with Image.open(io.BytesIO(data)) as image:
                if image.format != "JPEG" or image.size != (self.width, self.height):
                    raise ValueError("Camera returned unexpected image format or dimensions")
                image.load()
            await self.inspect()
            if epoch != self.generation or self.closed:
                raise RuntimeError("Camera identity changed; frame discarded")
            self.last_capture = time.time()
            self.capture_count += 1
            return VideoFrame(self.ident, data, self.width, self.height, self.last_capture, epoch)

    async def close(self):
        self.closed = True
        self.generation += 1
        processes = list(self.processes)
        for process in processes:
            if process.returncode is None:
                process.kill()
        await asyncio.gather(*(p.wait() for p in processes), return_exceptions=True)
