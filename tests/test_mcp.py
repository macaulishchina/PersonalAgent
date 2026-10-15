"""MCP domains, live device discovery, precise schemas and bounded IPC failures."""

import asyncio
import base64
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from fastmcp import Client
from fastmcp.exceptions import ToolError

from assistant_runtime.devices.directory import DeviceDirectory
from assistant_runtime.kernel.contracts import (
    Control,
    DeviceObservation,
    MemoryRequest,
    SensorReading,
    VideoCapture,
    CameraMotion,
    VideoRecord,
)
from assistant_runtime.mcp.catalog import MEMORY_TOOLS, DEVICE_TOOLS
from assistant_runtime.mcp.client import LocalRuntimeClient
from assistant_runtime.mcp.device import create_server as device_server
from assistant_runtime.mcp.memory import create_server as memory_server
from assistant_runtime.plugins.codex.mcp import server_config


class McpTest(unittest.IsolatedAsyncioTestCase):
    async def test_recording_tool_returns_references_without_loading_video_bytes(self):
        result = {"video": {"id": "a" * 32}, "attachments": [{"id": "a" * 32, "type": "video/mp4"}]}
        handler = SimpleNamespace(invoke=AsyncMock(return_value=result), status=AsyncMock())
        async with Client(device_server(handler)) as client:
            reply = await client.call_tool("record_video", {"device_id": "camera", "duration_seconds": 2})
            handler.invoke.assert_awaited_once_with(VideoRecord("camera", 2))
            self.assertEqual(reply.structured_content, result)
            self.assertTrue(all(block.type == "text" for block in reply.content))
            for duration in (0, 61, True):
                with self.assertRaises(ToolError):
                    await client.call_tool(
                        "record_video", {"device_id": "camera", "duration_seconds": duration}
                    )
            handler.invoke.assert_awaited_once()

    async def test_camera_motion_tools_are_typed_and_bounded(self):
        handler = SimpleNamespace(invoke=AsyncMock(return_value={"command_sent": True}), status=AsyncMock())
        async with Client(device_server(handler)) as client:
            await client.call_tool("move_camera", {"device_id": "camera", "direction": "left"})
            handler.invoke.assert_awaited_once_with(CameraMotion("camera", "left", 80))
            await client.call_tool("stop_camera", {"device_id": "camera"})
            self.assertEqual(handler.invoke.await_args.args[0], CameraMotion("camera", "stop", 0))
            for arguments in (
                {"direction": "zoom"},
                {"duration_ms": 0},
                {"duration_ms": 501},
                {"duration_ms": True},
            ):
                values = {"device_id": "camera", "direction": "left", **arguments}
                with self.assertRaises(ToolError):
                    await client.call_tool("move_camera", values)
            self.assertEqual(handler.invoke.await_count, 2)

    async def test_separate_inventory_annotations_and_typed_memory_requests(self):
        handler = SimpleNamespace(invoke=AsyncMock(return_value={"ok": True}), status=AsyncMock())
        async with Client(memory_server(handler)) as client:
            tools = {t.name: t for t in await client.list_tools()}
            self.assertEqual(set(tools), set(MEMORY_TOOLS))
            self.assertFalse(set(tools) & set(DEVICE_TOOLS))
            self.assertTrue(tools["search_memory"].annotations.read_only_hint)
            self.assertTrue(tools["forget_memory"].annotations.destructive_hint)
            self.assertNotIn("action", tools["remember_memory"].input_schema["properties"])
            await client.call_tool(
                "update_memory",
                {"memory_id": "fact", "revision": 2, "title": "偏好", "text": "新的偏好", "scope": "user"},
            )
            self.assertEqual(
                handler.invoke.await_args.args[0],
                MemoryRequest(
                    "remember",
                    {"id": "fact", "revision": 2, "title": "偏好", "text": "新的偏好", "scope": "user"},
                ),
            )
            with self.assertRaises(ToolError):
                await client.call_tool(
                    "update_memory",
                    {"memory_id": "fact", "revision": 0, "title": "偏好", "text": "坏修订", "scope": "user"},
                )
            handler.invoke.assert_awaited_once()
            with self.assertRaises(ToolError):
                await client.call_tool("list_devices", {})

    async def test_new_sensor_appears_without_rebuilding_mcp_or_hardware_branches(self):
        specs = {}
        directory = DeviceDirectory(specs)
        facade = SimpleNamespace(invoke=directory.handle, status=AsyncMock())
        async with Client(device_server(facade)) as client:
            tools = {t.name: t for t in await client.list_tools()}
            self.assertEqual(set(tools), set(DEVICE_TOOLS))
            self.assertTrue(tools["list_devices"].annotations.read_only_hint)
            self.assertTrue(tools["get_task_status"].annotations.read_only_hint)
            self.assertFalse(tools["capture_image"].annotations.read_only_hint)
            self.assertEqual((await client.call_tool("list_devices", {})).structured_content, {"devices": []})
            specs["new_temperature"] = {
                "adapter": "sensor",
                "kind": "sensor",
                "label": "测试温度计",
                "location": "测试",
                "connection": {"private": "not public"},
            }
            directory.register("sensor", "new_temperature", ["sensor.temperature"], AsyncMock())
            directory.observe(
                "sensor",
                "new_temperature",
                DeviceObservation(
                    "available",
                    readings={
                        "temperature": SensorReading("temperature", 23.5, "°C", "fixture", time.time())
                    },
                ),
            )
            listing = (await client.call_tool("list_devices", {})).structured_content
            self.assertEqual(listing["devices"][0]["id"], "new_temperature")
            self.assertNotIn("connection", listing["devices"][0])
            detail = (
                await client.call_tool("inspect_device", {"device_id": "new_temperature"})
            ).structured_content
            self.assertTrue(detail["device"]["fresh"])
            self.assertEqual(detail["device"]["readings"]["temperature"]["value"], 23.5)
            directory.observe(
                "sensor", "new_temperature", DeviceObservation("unavailable", error="Disconnected")
            )
            detail = (
                await client.call_tool("inspect_device", {"device_id": "new_temperature"})
            ).structured_content
            self.assertEqual(detail["device"]["state"], "unavailable")
            with self.assertRaises(ToolError):
                await client.call_tool("inspect_device", {"device_id": "missing"})

    async def test_camera_returns_native_image_without_changing_domain_data(self):
        original = {
            "frame": {
                "mime_type": "image/jpeg",
                "device_id": "test_camera",
                "data_base64": base64.b64encode(b"synthetic jpeg").decode(),
            }
        }
        handler = SimpleNamespace(invoke=AsyncMock(return_value=original), status=AsyncMock())
        async with Client(device_server(handler)) as client:
            result = await client.call_tool("capture_image", {"device_id": "test_camera"})
            self.assertTrue(any(b.type == "image" and b.mime_type == "image/jpeg" for b in result.content))
            self.assertNotIn("data_base64", result.structured_content["frame"])
            self.assertIn("data_base64", original["frame"])
            handler.invoke.assert_awaited_once_with(VideoCapture("test_camera"))

    async def test_stop_does_not_cancel_and_status_never_speaks(self):
        handler = SimpleNamespace(
            invoke=AsyncMock(return_value={"ok": True}),
            status=AsyncMock(
                return_value={"task_running": True, "task_progress": "执行中", "private": "not public"}
            ),
        )
        async with Client(device_server(handler)) as client:
            await client.call_tool("stop_playback", {})
            handler.invoke.assert_awaited_once_with(Control("stop"))
            state = await client.call_tool("get_task_status", {})
            self.assertEqual(state.structured_content, {"running": True, "progress": "执行中"})
            handler.invoke.assert_awaited_once()
            await client.call_tool("cancel_task", {})
            self.assertEqual(handler.invoke.await_args.args[0], Control("cancel"))
            with self.assertRaises(ToolError):
                await client.call_tool("set_audio_volume", {"percent": 101})
            self.assertEqual(handler.invoke.await_count, 2)

    async def test_remote_failure_does_not_become_a_success_or_replay(self):
        calls = []
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "capability.sock"

            async def serve(reader, writer):
                calls.append(json.loads(await reader.readline()))
                writer.write(b'{"ok":false,"error":"Unavailable"}\n')
                await writer.drain()
                writer.close()
                await writer.wait_closed()

            server = await asyncio.start_unix_server(serve, path=path)
            try:
                client = LocalRuntimeClient(path)
                with self.assertRaisesRegex(RuntimeError, "Unavailable"):
                    await client.invoke(Control("cancel"))
                self.assertEqual(calls, [{"command": "cancel", "value": None}])
            finally:
                server.close()
                await server.wait_closed()

    async def test_stalled_transport_is_bounded_and_closes_without_retry(self):
        finished = asyncio.Event()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "capability.sock"

            async def serve(reader, writer):
                await reader.readline()
                await reader.read()
                writer.close()
                await writer.wait_closed()
                finished.set()

            server = await asyncio.start_unix_server(serve, path=path)
            try:
                with self.assertRaises(TimeoutError):
                    await LocalRuntimeClient(path, timeout=0.05).invoke(
                        MemoryRequest("forget", {"id": "test"})
                    )
                await asyncio.wait_for(finished.wait(), 1)
            finally:
                server.close()
                await server.wait_closed()

    def test_codex_registers_two_scoped_services_and_maintenance_disables_them(self):
        cfg = server_config(Path("/tmp/private.sock"))
        self.assertEqual(set(cfg), {"memory", "device"})
        self.assertEqual(set(cfg["memory"]["enabled_tools"]), set(MEMORY_TOOLS))
        self.assertEqual(set(cfg["device"]["enabled_tools"]), set(DEVICE_TOOLS))
        self.assertTrue(all(x["enabled"] for x in cfg.values()))
        self.assertTrue(all(x["default_tools_approval_mode"] == "approve" for x in cfg.values()))
        self.assertTrue(
            all(not x["enabled"] for x in server_config("/tmp/private.sock", enabled=False).values())
        )
        self.assertNotEqual(cfg["memory"]["args"], cfg["device"]["args"])


if __name__ == "__main__":
    unittest.main()
