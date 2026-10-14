"""Bounded on-demand snapshots from a configured go2rtc stream."""

import asyncio
import hashlib
import io
import json
import math
from pathlib import Path
import re
import time
from urllib.parse import urlsplit

import aiohttp
from PIL import Image

from ...kernel.contracts import VideoFrame


class GatewayUnavailable(RuntimeError):
    pass


def validate_connection(spec):
    values = spec["connection"]
    allowed = {"base_url", "stream", "timeout_seconds", "username", "password_file"}
    if set(values) - allowed:
        raise ValueError("Unknown go2rtc connection settings")
    base = values.get("base_url")
    if not isinstance(base, str):
        raise ValueError("go2rtc requires a gateway base_url")
    try:
        url = urlsplit(base)
        valid = (
            url.scheme in {"http", "https"}
            and url.hostname
            and url.username is None
            and url.password is None
            and url.path in {"", "/"}
            and not url.query
            and not url.fragment
        )
        url.port
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("Invalid go2rtc gateway URL; credentials belong in a private file")
    stream = values.get("stream")
    if not isinstance(stream, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", stream):
        raise ValueError("go2rtc requires an exact registered stream name")
    timeout = values.get("timeout_seconds", 8)
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 10:
        raise ValueError("Invalid go2rtc capture timeout")
    if ("username" in values) != ("password_file" in values):
        raise ValueError("go2rtc username and password_file must be configured together")
    if "username" in values and (
        not isinstance(values["username"], str)
        or not values["username"]
        or ":" in values["username"]
        or not isinstance(values["password_file"], str)
        or not Path(values["password_file"]).is_absolute()
    ):
        raise ValueError("Invalid go2rtc credential configuration")


def decode_jpeg(data):
    try:
        with Image.open(io.BytesIO(data)) as image:
            width, height = image.size
            if image.format != "JPEG" or not 0 < width <= 1920 or not 0 < height <= 1080:
                raise ValueError("go2rtc returned an unexpected image format or dimensions")
            image.load()
            return width, height
    except OSError as exc:
        raise ValueError("go2rtc returned an invalid JPEG") from exc


class Go2RTCCamera:
    def __init__(self, device_id, spec):
        validate_connection(spec)
        self.device_id = device_id
        self.options = spec["connection"]
        self.base_url = self.options["base_url"].rstrip("/")
        self.stream = self.options["stream"]
        self.timeout = self.options.get("timeout_seconds", 8)
        self.session = None
        self.lock = asyncio.Lock()
        self.operations = set()
        self.generation = 1
        self.identity = None
        self.closed = False
        self.last_error = None
        self.last_capture = None
        self.capture_generation = None
        self.capture_count = 0

    async def start(self):
        headers = {}
        if "password_file" in self.options:

            def read_password():
                with Path(self.options["password_file"]).open() as handle:
                    return handle.read(4097).rstrip("\r\n")

            password = await asyncio.to_thread(read_password)
            if not password or len(password) > 4096:
                raise ValueError("Invalid go2rtc password file")
            headers["Authorization"] = aiohttp.encode_basic_auth(self.options["username"], password)
        self.session = aiohttp.ClientSession(
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=self.timeout, connect=min(2, self.timeout)),
            connector=aiohttp.TCPConnector(limit=2),
            cookie_jar=aiohttp.DummyCookieJar(),
            trust_env=False,
            auto_decompress=False,
        )

    async def read(self, endpoint, limit, image=False):
        if self.closed or self.session is None:
            raise RuntimeError("go2rtc camera provider closed or not started")
        try:
            async with self.session.get(
                self.base_url + endpoint,
                params={"src": self.stream},
                allow_redirects=False,
                headers={"Cache-Control": "no-cache"},
            ) as response:
                if response.status in {401, 403}:
                    raise GatewayUnavailable("go2rtc access denied")
                if response.status == 404:
                    raise RuntimeError("Configured go2rtc stream not found")
                if response.status != 200:
                    # Never copy server error bodies: they can contain source credentials.
                    raise RuntimeError(f"go2rtc request failed (HTTP {response.status})")
                if image and response.content_type != "image/jpeg":
                    raise ValueError("go2rtc did not return JPEG content")
                if response.content_length is not None and response.content_length > limit:
                    raise ValueError("go2rtc response exceeded its size limit")
                data = bytearray()
                async for block in response.content.iter_chunked(65536):
                    data.extend(block)
                    if len(data) > limit:
                        raise ValueError("go2rtc response exceeded its size limit")
                return bytes(data)
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise GatewayUnavailable("go2rtc gateway unreachable or request timed out") from exc

    async def source_identity(self):
        data = await self.read("/api/streams", 65536)
        try:
            metadata = json.loads(data)
            producers = metadata["producers"]
            urls = [p["url"] for p in producers]
            if not urls or any(not isinstance(url, str) or not url for url in urls):
                raise ValueError
        except (ValueError, KeyError, TypeError) as exc:
            raise ValueError("go2rtc returned invalid stream metadata") from exc
        identity = hashlib.sha256(json.dumps(urls).encode()).hexdigest()
        if identity != self.identity:
            self.identity = identity
            self.generation += 1
            self.last_error = None
        return identity

    def invalidate(self):
        if self.identity is not None:
            self.identity = None
            self.generation += 1

    async def inspect(self):
        try:
            async with asyncio.timeout(min(2, self.timeout)):
                await self.source_identity()
        except (RuntimeError, ValueError, TimeoutError) as exc:
            self.invalidate()
            self.last_error = "go2rtc probe timed out" if isinstance(exc, TimeoutError) else str(exc)
            raise
        return {
            "capturing": self.lock.locked(),
            "capture_mode": "on_request",
            "transport": "go2rtc",
            "stream": self.stream,
            "gateway_reachable": True,
            "stream_configured": True,
            "capture_verified": self.capture_generation == self.generation and self.last_error is None,
            "capture_count": self.capture_count,
            "last_capture_at": self.last_capture,
            "raw_video_saved": False,
        }

    async def capture(self):
        if self.closed:
            raise RuntimeError("go2rtc camera provider closed")
        if self.lock.locked():
            raise RuntimeError("Camera busy; another acquisition is in progress")
        task = asyncio.current_task()
        self.operations.add(task)
        try:
            async with self.lock, asyncio.timeout(self.timeout):
                identity = await self.source_identity()
                epoch = self.generation
                data = await self.read("/api/frame.jpeg", 1024 * 1024, image=True)
                width, height = await asyncio.to_thread(decode_jpeg, data)
                after = await self.source_identity()
                if self.closed or identity != after or epoch != self.generation:
                    raise RuntimeError("go2rtc source or generation changed; frame discarded")
                result = VideoFrame(self.device_id, data, width, height, time.time(), epoch)
                self.capture_count += 1
                self.last_capture = result.captured_at
                self.capture_generation = epoch
                self.last_error = None
                return result
        except BaseException as exc:
            if isinstance(exc, (GatewayUnavailable, TimeoutError)):
                self.invalidate()
            self.last_error = (
                "Capture interrupted"
                if isinstance(exc, asyncio.CancelledError)
                else "go2rtc capture timed out"
                if isinstance(exc, TimeoutError)
                else str(exc)[:200]
            )
            raise
        finally:
            self.operations.discard(task)

    async def close(self):
        self.closed = True
        self.generation += 1
        operations = [task for task in self.operations if task is not asyncio.current_task()]
        for task in operations:
            task.cancel()
        await asyncio.gather(*operations, return_exceptions=True)
        if self.session:
            await self.session.close()
