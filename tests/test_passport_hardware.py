"""Acceptance-rig safeguards and real-Pi fixtures, without USB or paid providers."""

import asyncio
import base64
from collections import deque
import hashlib
import json
import os
from pathlib import Path
import socket
import struct
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from check_passport_hardware import SpeechAllowance, speech_cost
from assistant_runtime.api.ai_usage import UsageScope, UsageError
from assistant_runtime.core.ai_usage.service import UsageService
from assistant_runtime.core.ai_usage.settings import UsageSettings
from aiohttp import ClientSession, WSMsgType


class AllowanceTest(unittest.IsolatedAsyncioTestCase):
    async def test_original_ledger_and_other_reservations_are_counted_before_every_dispatch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            other = UsageService(
                root / "other",
                UsageSettings.parse(
                    {
                        "prices": {
                            "tts": {
                                "provider": "aliyun_speech",
                                "model": "cosyvoice-v3-flash",
                                "text_cny_per_character": 0.001,
                            }
                        }
                    }
                ),
            )
            original = UsageService(root / "original", UsageSettings.parse({}))
            await other.start()
            await original.start()
            await original.close()
            budget = SpeechAllowance(root / "original", root / "other/usage.sqlite3")
            await budget.usage.start()
            try:
                scope = UsageScope("fixture", "terminal:fixture")
                ticket = await budget.admit(
                    scope,
                    service="speech_tts",
                    provider="aliyun_speech",
                    model="cosyvoice-v3-flash",
                    bounds={"text_characters": 100},
                )
                await budget.settle(ticket["id"], state="unknown")
                self.assertAlmostEqual(speech_cost(budget.ledger), 0.1)
                ticket = await other.admit(
                    scope,
                    service="speech_tts",
                    provider="aliyun_speech",
                    model="cosyvoice-v3-flash",
                    bounds={"text_characters": 10000},
                )
                await other.settle(ticket["id"], state="completed", usage={"text_characters": 10000})
                with self.assertRaisesRegex(UsageError, "allowance_exhausted"):
                    await budget.admit(
                        scope,
                        service="speech_tts",
                        provider="aliyun_speech",
                        model="cosyvoice-v3-flash",
                        bounds={"text_characters": 1},
                    )
                self.assertAlmostEqual(speech_cost(budget.ledger), 0.1)
            finally:
                await budget.usage.close()
                await other.close()

    async def test_missing_ledgers_cannot_create_a_fresh_allowance(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                SpeechAllowance(Path(directory), Path(directory) / "missing.sqlite3")


@unittest.skipUnless(sys.platform == "linux", "Requires Linux-certified speech_io")
class RigTest(unittest.IsolatedAsyncioTestCase):
    async def test_image_then_task_completion_through_real_pi_and_bounded_terminal_protocol(self):
        with tempfile.TemporaryDirectory(prefix="pp-rig-") as directory:
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "scripts/check_passport_hardware.py",
                "--root",
                directory,
                "--device-id",
                "0" * 32,
                "--port",
                str(port),
                "--image-fixture",
                "--task-fixture",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env={**os.environ, "PYTHONPATH": "src"},
            )
            output, ready = deque(maxlen=40), asyncio.Event()
            seen = deque(maxlen=30)

            async def drain():
                async for line in process.stdout:
                    output.append(line.decode(errors="replace").strip())
                    if b"Hardware rig ready:" in line:
                        ready.set()

            reader = asyncio.create_task(drain())
            try:
                await asyncio.wait_for(ready.wait(), 40)
                base = f"http://127.0.0.1:{port}/entrances/ai_passport"
                async with ClientSession() as client:
                    body = {"device_id": "0" * 32, "credential": "K" * 64, "protocol": 2}
                    async with client.post(base + "/pair", json={**body, "device_id": "1" * 32}) as response:
                        self.assertEqual(response.status, 403)
                    for _ in range(2):
                        async with client.post(base + "/pair", json=body) as response:
                            self.assertEqual(response.status, 200)
                            paired = await response.json()
                    self.assertTrue(paired["paired"])
                    headers = {
                        "X-Device-Id": body["device_id"],
                        "Authorization": "Bearer " + body["credential"],
                    }
                    async with client.ws_connect(base + "/stream", headers=headers) as ws:
                        first = "11111111-1111-4111-8111-111111111111"
                        await ws.send_json({"type": "input.begin", "request_id": first, "mode": "steer"})
                        image_meta = None
                        image_data = bytearray()
                        image_done = sent_second = got_notice = False
                        request_id = first
                        async with asyncio.timeout(50):
                            while True:
                                event = await ws.receive()
                                self.assertIn(event.type, (WSMsgType.TEXT, WSMsgType.BINARY))
                                if event.type == WSMsgType.BINARY:
                                    generation = struct.unpack("!I", event.data[:4])[0]
                                    await ws.send_json(
                                        {
                                            "type": "playback.credit",
                                            "generation": generation,
                                            "bytes": len(event.data) - 4,
                                        }
                                    )
                                    continue
                                value = json.loads(event.data)
                                kind = value["type"]
                                if kind == "message":
                                    self.assertIn(value["role"], ("assistant", "user"))
                                    self.assertGreaterEqual(
                                        value["total"], value["offset"] + len(value["text"])
                                    )
                                seen.append((kind, value.get("state"), value.get("text", "")[-100:]))
                                self.assertNotEqual(kind, "error", value)
                                if kind == "input.ready":
                                    await ws.send_bytes(struct.pack("!I", 0) + b"\0\0" * 320)
                                    await ws.send_json(
                                        {"type": "input.finish", "request_id": request_id, "frames": 1}
                                    )
                                elif kind == "message" and value.get("images") and not image_meta:
                                    await ws.send_json(
                                        {
                                            "type": "image.open",
                                            "message_id": value["message_id"],
                                            "image_id": value["images"][0]["id"],
                                            "width": 96,
                                            "height": 64,
                                        }
                                    )
                                elif kind == "image.open":
                                    image_meta = value
                                    await ws.send_json(
                                        {"type": "image.chunk", "version": value["version"], "offset": 0}
                                    )
                                elif kind == "image.chunk":
                                    image_data.extend(base64.b64decode(value["data"]))
                                    if value["next_offset"] is None:
                                        self.assertEqual(len(image_data), image_meta["bytes"])
                                        self.assertEqual(
                                            hashlib.sha256(image_data).hexdigest(), image_meta["version"]
                                        )
                                        image_done = True
                                        await ws.send_json({"type": "image.close"})
                                    else:
                                        await ws.send_json(
                                            {
                                                "type": "image.chunk",
                                                "version": value["version"],
                                                "offset": value["next_offset"],
                                            }
                                        )
                                elif kind == "playback.start":
                                    await ws.send_json(
                                        {
                                            "type": "playback.credit",
                                            "generation": value["generation"],
                                            "bytes": 8192,
                                        }
                                    )
                                elif kind == "playback.end":
                                    await ws.send_json(
                                        {"type": "playback.completed", "generation": value["generation"]}
                                    )
                                elif kind == "playback.finished" and not sent_second and image_done:
                                    sent_second = True
                                    request_id = "22222222-2222-4222-8222-222222222222"
                                    await ws.send_json(
                                        {"type": "input.begin", "request_id": request_id, "mode": "queue"}
                                    )
                                elif kind == "notifications" and value["items"]:
                                    item = value["items"][0]
                                    await ws.send_json(
                                        {
                                            "type": "notification.page",
                                            "id": item["id"],
                                            "revision": item["revision"],
                                            "offset": 0,
                                        }
                                    )
                                elif kind == "notification.page":
                                    self.assertTrue(value["text"])
                                    got_notice = True
                                    break
                        self.assertTrue(image_done and sent_second and got_notice)
            except TimeoutError:
                self.fail(f"Rig timed out; last events={list(seen)}; log={list(output)}")
            finally:
                if process.returncode is None:
                    process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), 20)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()
                await reader
                self.assertEqual(process.returncode, 0, "\n".join(output))
