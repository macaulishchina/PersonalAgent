"""Gateway HTTP contracts: bounded images, source identity, auth and cleanup."""

import asyncio
import io
from pathlib import Path
import tempfile
import unittest

import aiohttp
from aiohttp import web
from PIL import Image

from assistant_runtime.plugins.go2rtc.camera import Go2RTCCamera, GatewayUnavailable


def jpeg(size=(640, 480)):
    output = io.BytesIO()
    Image.new("RGB", size, "red").save(output, format="JPEG")
    return output.getvalue()


class Go2RTCTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.source = "rtsp://admin:private-source-password@camera/live"
        self.image = jpeg()
        self.content_type = "image/jpeg"
        self.status = 200
        self.metadata_status = 200
        self.block = False
        self.chunked = False
        self.change_source = False
        self.auth = None
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.queries = []
        self.snapshot_requests = 0
        app = web.Application()
        app.router.add_get("/api/streams", self.metadata)
        app.router.add_get("/api/frame.jpeg", self.snapshot)
        self.runner = web.AppRunner(app, handler_cancellation=True)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.base = f"http://127.0.0.1:{self.runner.addresses[0][1]}"
        self.camera = Go2RTCCamera("camera", self.spec())
        await self.camera.start()

    def spec(self, **options):
        return {"connection": {"base_url": self.base, "stream": "camera", **options}}

    async def asyncTearDown(self):
        self.release.set()
        await self.camera.close()
        await self.runner.cleanup()

    async def metadata(self, request):
        self.queries.append(dict(request.query))
        if self.auth and request.headers.get("Authorization") != self.auth:
            return web.Response(status=401)
        if self.metadata_status != 200:
            return web.Response(status=self.metadata_status, text=self.source)
        return web.json_response({"producers": [{"url": self.source}], "consumers": []})

    async def snapshot(self, request):
        self.queries.append(dict(request.query))
        self.snapshot_requests += 1
        self.started.set()
        if self.block:
            await self.release.wait()
        if self.change_source:
            self.source += "-changed"
        if self.status != 200:
            return web.Response(status=self.status, text=self.source)
        if self.chunked:
            response = web.StreamResponse(headers={"Content-Type": "image/jpeg"})
            await response.prepare(request)
            try:
                for _ in range(18):
                    await response.write(b"x" * 65536)
                await response.write_eof()
            except ConnectionResetError:
                pass
            return response
        return web.Response(body=self.image, content_type=self.content_type)

    async def test_observation_never_starts_capture_or_exposes_credentials(self):
        state = await self.camera.inspect()
        self.assertFalse(state["capturing"])
        self.assertFalse(state["capture_verified"])
        self.assertEqual(self.snapshot_requests, 0)
        self.assertNotIn("private-source-password", str(state))
        self.assertTrue(all(query == {"src": "camera"} for query in self.queries))

    async def test_fresh_frames_use_registered_stream_without_cache_and_are_not_retained(self):
        first = await self.camera.capture()
        second = await self.camera.capture()
        self.assertEqual((first.width, first.height), (640, 480))
        self.assertEqual(first.device_id, "camera")
        self.assertGreater(second.captured_at, first.captured_at)
        self.assertEqual(self.camera.capture_count, 2)
        self.assertEqual(self.snapshot_requests, 2)
        self.assertTrue(all(query == {"src": "camera"} for query in self.queries))
        self.assertFalse(any(isinstance(value, bytes) for value in vars(self.camera).values()))
        self.assertTrue((await self.camera.inspect())["capture_verified"])

    async def test_server_failure_does_not_reuse_a_frame_or_expose_error_body(self):
        await self.camera.capture()
        self.status = 500
        with self.assertRaisesRegex(RuntimeError, "HTTP 500") as failure:
            await self.camera.capture()
        self.assertNotIn("private-source-password", str(failure.exception))
        self.assertEqual(self.camera.capture_count, 1)
        self.assertFalse((await self.camera.inspect())["capture_verified"])
        self.status = 200
        await self.camera.capture()
        self.assertTrue((await self.camera.inspect())["capture_verified"])

    async def test_malformed_wrong_type_and_oversized_dimensions_are_rejected(self):
        for data, content_type in (
            (b"", "image/jpeg"),
            (b"invalid", "image/jpeg"),
            (jpeg(), "image/png"),
            (jpeg((1921, 480)), "image/jpeg"),
        ):
            with self.subTest(content_type=content_type, size=len(data)):
                self.image, self.content_type = data, content_type
                with self.assertRaises(ValueError):
                    await self.camera.capture()
        self.assertEqual(self.camera.capture_count, 0)

    async def test_known_and_unknown_content_length_are_bounded(self):
        self.image = b"x" * (1024 * 1024 + 1)
        with self.assertRaisesRegex(ValueError, "size limit"):
            await self.camera.capture()
        self.chunked = True
        with self.assertRaisesRegex(ValueError, "size limit"):
            await self.camera.capture()
        self.assertEqual(self.camera.capture_count, 0)

    async def test_missing_stream_and_gateway_recovery_invalidate_old_verification(self):
        await self.camera.capture()
        generation = self.camera.generation
        for status in (404, 401):
            self.metadata_status = status
            with self.assertRaises(RuntimeError):
                await self.camera.inspect()
        self.metadata_status = 200
        self.assertFalse((await self.camera.inspect())["capture_verified"])
        self.assertGreater(self.camera.generation, generation)
        await self.camera.capture()
        self.assertTrue((await self.camera.inspect())["capture_verified"])

    async def test_source_change_during_acquisition_discards_frame(self):
        self.change_source = True
        with self.assertRaisesRegex(RuntimeError, "source or generation changed"):
            await self.camera.capture()
        self.assertEqual(self.camera.capture_count, 0)
        self.change_source = False
        await self.camera.capture()

    async def test_timeout_closes_request_and_releases_capture(self):
        await self.camera.close()
        self.camera = Go2RTCCamera("camera", self.spec(timeout_seconds=0.15))
        await self.camera.start()
        self.block = True
        with self.assertRaises((TimeoutError, GatewayUnavailable)):
            await self.camera.capture()
        self.assertFalse(self.camera.lock.locked())
        self.assertFalse(self.camera.operations)
        self.assertEqual(self.camera.capture_count, 0)

    async def test_cancel_close_and_concurrent_capture_do_not_deliver_late_frames(self):
        self.block = True
        task = asyncio.create_task(self.camera.capture())
        await asyncio.wait_for(self.started.wait(), 2)
        with self.assertRaisesRegex(RuntimeError, "busy"):
            await self.camera.capture()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(self.camera.operations)
        self.started.clear()
        task = asyncio.create_task(self.camera.capture())
        await asyncio.wait_for(self.started.wait(), 2)
        await self.camera.close()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(self.camera.session.closed)
        self.assertFalse(self.camera.lock.locked())
        self.assertEqual(self.camera.capture_count, 0)

    async def test_private_basic_auth_and_redirect_rejection(self):
        await self.camera.close()
        with tempfile.TemporaryDirectory() as temp:
            password = Path(temp) / "password"
            password.write_text("test-password\n")
            self.auth = aiohttp.encode_basic_auth("admin", "test-password")
            self.camera = Go2RTCCamera("camera", self.spec(username="admin", password_file=str(password)))
            await self.camera.start()
            await self.camera.capture()
            self.metadata_status = 302
            with self.assertRaisesRegex(RuntimeError, "HTTP 302"):
                await self.camera.inspect()
