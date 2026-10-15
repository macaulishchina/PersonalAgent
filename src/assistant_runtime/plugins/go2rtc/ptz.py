"""Bounded directional control over the gateway's existing camera session."""

import asyncio
from contextlib import asynccontextmanager
import json
import time

import aiohttp

from .camera import GatewayUnavailable


class Go2RTCPanTilt:
    def __init__(self, camera):
        self.camera = camera
        settings = camera.options["pan_tilt"]
        self.axes = tuple(settings["axes"])
        self.maximum = settings.get("max_duration_ms", 500)
        self.active = None
        self.operations = set()
        self.closed = False
        self.pending_stops = 0
        self.stop_lock = asyncio.Lock()
        self.last_stop_sent = False
        self.last_error = None
        self.last_command_at = None

    def state(self):
        return {
            "axes": list(self.axes),
            "max_duration_ms": self.maximum,
            "command_in_progress": self.active is not None,
            "position_feedback": False,
            "last_command_at": self.last_command_at,
            "last_error": self.last_error,
        }

    async def post(self, direction, duration=0):
        async with asyncio.timeout(2):
            data = await self.camera.read(
                "/api/xiaomi/ptz",
                4096,
                method="POST",
                params={"src": self.camera.stream, "direction": direction, "duration": duration},
            )
        try:
            result = json.loads(data)
            if result != {"src": self.camera.stream, "direction": direction, "duration": duration}:
                raise ValueError
        except (ValueError, TypeError):
            raise ValueError("go2rtc returned an invalid camera control response") from None

    @asynccontextmanager
    async def connection(self):
        """Keep a bounded, discarded MP4 consumer alive; never record or decode it."""
        if self.closed or self.camera.session is None:
            raise RuntimeError("Camera controller closed or not started")
        response = None
        drain = None
        try:
            response = await self.camera.session.get(
                self.camera.base_url + "/api/stream.mp4",
                params={"src": self.camera.stream, "video": "h264,h265"},
                allow_redirects=False,
            )
            if response.status != 200 or response.content_type != "video/mp4":
                raise GatewayUnavailable("go2rtc camera control connection unavailable")

            async def discard():
                received = 0
                async for block in response.content.iter_chunked(65536):
                    received += len(block)
                    if received > 32 * 1024 * 1024:
                        raise GatewayUnavailable("Camera control media exceeded its bound")

            drain = asyncio.create_task(discard(), name="go2rtc:control-media")
            yield drain
        except aiohttp.ClientError as exc:
            raise GatewayUnavailable("go2rtc camera control connection lost") from exc
        finally:
            if response is not None:
                response.close()
            if drain is not None:
                drain.cancel()
                await asyncio.gather(drain, return_exceptions=True)

    def result(self, direction, duration, sent=True):
        return {
            "device_id": self.camera.device_id,
            "direction": direction,
            "duration_ms": duration,
            "command_sent": sent,
            "stop_sent": self.last_stop_sent,
            "position_verified": False,
        }

    async def move(self, request):
        if self.closed:
            raise RuntimeError("Camera controller closed")
        if request.direction == "stop":
            return await self.stop()
        axis = "pan" if request.direction in {"left", "right"} else "tilt"
        if axis not in self.axes or request.duration_ms > self.maximum:
            raise ValueError("Camera axis or movement duration is not supported")
        if self.active is not None or self.pending_stops:
            raise RuntimeError("Camera busy; another movement or stop is in progress")
        task = asyncio.current_task()
        self.active = task
        self.operations.add(task)
        self.last_stop_sent = False
        self.last_error = None
        try:
            async with asyncio.timeout(self.camera.timeout):
                identity = await self.camera.source_identity()
                epoch = self.camera.generation
                async with self.connection() as media:
                    attempted = False
                    try:
                        if media.done():
                            raise GatewayUnavailable("Camera control media ended before movement")
                        if identity != await self.camera.source_identity() or epoch != self.camera.generation:
                            raise RuntimeError("Camera source changed before movement")
                        attempted = True  # A failed HTTP call may still have moved the camera.
                        await self.post(request.direction, request.duration_ms)
                        self.last_command_at = time.time()
                        if identity != await self.camera.source_identity() or epoch != self.camera.generation:
                            raise RuntimeError("Camera source changed; movement outcome unconfirmed")
                    finally:
                        if attempted:
                            try:
                                await self.post("stop")
                                self.last_stop_sent = True
                            except (RuntimeError, ValueError, TimeoutError, aiohttp.ClientError):
                                self.last_error = "Camera stop unconfirmed; movement will not be retried"
                if not self.last_stop_sent:
                    raise GatewayUnavailable("Camera stop unconfirmed; movement will not be retried")
                return self.result(request.direction, request.duration_ms)
        except BaseException as exc:
            if self.last_error is None:
                self.last_error = (
                    "Camera movement interrupted"
                    if isinstance(exc, asyncio.CancelledError)
                    else (
                        "Camera control timed out; outcome unconfirmed"
                        if isinstance(exc, TimeoutError)
                        else str(exc)[:200]
                    )
                )
            if isinstance(exc, TimeoutError):
                raise GatewayUnavailable(self.last_error) from exc
            raise
        finally:
            self.active = None
            self.operations.discard(task)

    async def stop(self):
        task = asyncio.current_task()
        self.operations.add(task)
        self.pending_stops += 1
        try:
            async with self.stop_lock:
                active = self.active
                if active is not None and active is not task:
                    if not active.cancelling():
                        active.cancel()
                    await asyncio.shield(asyncio.gather(active, return_exceptions=True))
                    if self.last_stop_sent:
                        self.last_error = None
                        return self.result("stop", 0)
                async with asyncio.timeout(self.camera.timeout), self.connection():
                    await self.post("stop")
                self.last_stop_sent = True
                self.last_command_at = time.time()
                self.last_error = None
                return self.result("stop", 0)
        except (RuntimeError, ValueError, TimeoutError, aiohttp.ClientError):
            self.last_stop_sent = False
            self.last_error = "Camera stop unconfirmed"
            raise GatewayUnavailable(self.last_error) from None
        finally:
            self.pending_stops -= 1
            self.operations.discard(task)

    async def close(self):
        self.closed = True
        tasks = [task for task in self.operations if task is not asyncio.current_task()]
        for task in tasks:
            if not task.cancelling():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
