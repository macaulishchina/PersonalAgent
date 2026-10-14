"""Isolated real memory/device MCP and subscription acceptance; no room IO or user data."""

import argparse
import asyncio
import base64
from dataclasses import asdict, replace
import io
import json
from pathlib import Path
import sys
import tempfile
import time

from fastmcp import Client
from fastmcp.exceptions import ToolError
from PIL import Image

from assistant_runtime.config import Config
from assistant_runtime.devices.directory import DeviceDirectory, DevicesPlugin
from assistant_runtime.kernel.contracts import (
    Control,
    DeviceObservation,
    DeviceQuery,
    MemoryRequest,
    SensorReading,
    VideoCapture,
    VideoFrame,
)
from assistant_runtime.kernel.runtime import Runtime, Manifest
from assistant_runtime.mcp.catalog import MEMORY_TOOLS, DEVICE_TOOLS
from assistant_runtime.memory.mem0 import Mem0Memory
from assistant_runtime.memory.service import MemoryPlugin
from assistant_runtime.plugins.codex.backend import CodexBackend
from assistant_runtime.plugins.codex.mcp import server_config
from assistant_runtime.store import Store


def client_for(name, socket):
    return Client(
        {
            "mcpServers": {
                name: {
                    "command": sys.executable,
                    "args": ["-m", "assistant_runtime.mcp", name, "--socket", str(socket)],
                }
            }
        }
    )


async def verify(config):
    evidence = config.state_dir / "acceptance/mcp"
    evidence.mkdir(parents=True, exist_ok=True)
    result = {"passed": False, "started": time.time(), "synthetic_only": True}
    with tempfile.TemporaryDirectory(prefix="mcp-acceptance-", dir=config.state_dir) as temp:
        root = Path(temp)
        isolated = replace(
            config,
            root=root,
            workspace=str(root),
            devices={},
            memory_auto_capture=False,
            codex_sandbox="read-only",
        )
        store = Store(isolated.state_dir / "assistant.sqlite3")

        async def quiet(*args):
            pass

        backend = CodexBackend(isolated, store, quiet, quiet)
        runtime = Runtime(store)
        directory = DeviceDirectory(isolated.devices)
        provider = Mem0Memory(
            root / "mem0", config.models / "memory-embedding", backend.memory_complete, str(root)
        )
        memory = MemoryPlugin(isolated, store, provider, lambda: backend.busy)
        runtime.add(memory)
        runtime.add(DevicesPlugin(directory))
        controls = []

        class CaptureOwner:
            manifest = Manifest("acceptance-controls")

            async def start(self, context):
                async def capture(request):
                    if request.device_id != "synthetic_camera":
                        raise ValueError("Unknown camera")
                    image = io.BytesIO()
                    Image.new("RGB", (64, 64), "blue").save(image, format="JPEG")
                    return VideoFrame(request.device_id, image.getvalue(), 64, 64, time.time(), 1)

                async def control(request):
                    controls.append(asdict(request))
                    return {"ok": True}

                context.register(VideoCapture, capture)
                context.register(Control, control)

        runtime.add(CaptureOwner())

        async def connection(reader, writer):
            try:
                d = json.loads(await reader.readline())
                if d["command"] == "memory":
                    data = await runtime.invoke(MemoryRequest(d["action"], d["arguments"]), timeout=90)
                elif d["command"] == "devices":
                    if d["action"] == "snapshot":
                        frame = await runtime.invoke(VideoCapture(d["device_id"]))
                        metadata = asdict(frame)
                        metadata.pop("data")
                        data = {"frame": {**metadata, "data_base64": base64.b64encode(frame.data).decode()}}
                    else:
                        data = await runtime.invoke(DeviceQuery(d["action"], d.get("device_id")))
                elif d["command"] == "status":
                    data = {"task_running": backend.busy, "task_progress": backend.progress}
                else:
                    data = await runtime.invoke(Control(d["command"], d.get("value")))
                reply = {"ok": True, "result": data}
            except Exception as e:
                reply = {"ok": False, "error": str(e)}
            writer.write((json.dumps(reply) + "\n").encode())
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        server = None
        calls = []
        original_get = backend.client.events.get

        async def trace():
            event = await original_get()
            item = event.get("params", {}).get("item", {})
            if event.get("method") == "item/completed" and item.get("type") == "mcpToolCall":
                calls.append(
                    {"server": item.get("server"), "tool": item.get("tool"), "status": item.get("status")}
                )
            return event

        backend.client.events.get = trace
        try:
            await runtime.start()
            server = await asyncio.start_unix_server(connection, path=isolated.socket)
            await backend.start()
            assert (await backend.account()).get("type") == "chatgpt"
            print("real stdio inventories and isolated Mem0 CRUD", flush=True)
            async with (
                client_for("memory", isolated.socket) as mc,
                client_for("device", isolated.socket) as dc,
            ):
                result["tool_inventory"] = {
                    "memory": [t.name for t in await mc.list_tools()],
                    "device": [t.name for t in await dc.list_tools()],
                }
                assert set(result["tool_inventory"]["memory"]) == set(MEMORY_TOOLS)
                assert set(result["tool_inventory"]["device"]) == set(DEVICE_TOOLS)
                saved = (
                    await mc.call_tool(
                        "remember_memory",
                        {"title": "MCP验收偏好", "text": "本次验收的主题颜色是蓝色。", "scope": "project"},
                    )
                ).structured_content["record"]
                ident = saved["id"]
                assert (await mc.call_tool("read_memory", {"memory_id": ident})).structured_content["record"][
                    "revision"
                ] == 1
                updated = (
                    await mc.call_tool(
                        "update_memory",
                        {
                            "memory_id": ident,
                            "revision": 1,
                            "title": "MCP验收偏好",
                            "text": "本次验收的主题颜色是绿色。",
                            "scope": "project",
                        },
                    )
                ).structured_content["record"]
                assert updated["revision"] == 2
                try:
                    await mc.call_tool(
                        "update_memory",
                        {
                            "memory_id": ident,
                            "revision": 1,
                            "title": "MCP验收偏好",
                            "text": "不允许覆盖的新数据",
                            "scope": "project",
                        },
                    )
                except ToolError:
                    pass
                else:
                    raise AssertionError("Stale memory revision was accepted")
                found = (
                    await mc.call_tool("search_memory", {"query": "主题颜色", "scope": "project"})
                ).structured_content
                assert any(r["id"] == ident and "绿色" in r["text"] for r in found["records"])
                await mc.call_tool("forget_memory", {"memory_id": ident})
                assert (await mc.call_tool("read_memory", {"memory_id": ident})).structured_content[
                    "record"
                ] is None
                await mc.call_tool(
                    "remember_memory",
                    {"title": "MCP验收偏好", "text": "本次验收的主题颜色是绿色。", "scope": "project"},
                )
                result["memory_crud_and_revision_guard"] = True
                print("same device MCP discovers late sensor/camera registration", flush=True)
                assert (await dc.call_tool("list_devices", {})).structured_content == {"devices": []}
                for ident, kind, caps in (
                    ("synthetic_temperature", "sensor", ["sensor.temperature"]),
                    ("synthetic_camera", "camera", ["video.snapshot"]),
                ):
                    isolated.devices[ident] = {
                        "adapter": "synthetic",
                        "kind": kind,
                        "label": "验收模拟" + kind,
                        "location": "验收",
                        "connection": {},
                    }
                    directory.register("synthetic", ident, caps, quiet)
                    directory.observe(
                        "synthetic",
                        ident,
                        DeviceObservation(
                            "available",
                            {"synthetic": True},
                            readings={
                                "temperature": SensorReading(
                                    "temperature", 23.5, "°C", "synthetic fixture", time.time()
                                )
                            }
                            if kind == "sensor"
                            else {},
                        ),
                    )
                listing = (await dc.call_tool("list_devices", {})).structured_content["devices"]
                assert len(listing) == 2
                inspected = (
                    await dc.call_tool("inspect_device", {"device_id": "synthetic_temperature"})
                ).structured_content["device"]
                assert (
                    inspected["readings"]["temperature"]["value"] == 23.5
                    and inspected["readings"]["temperature"]["fresh"]
                )
                image = await dc.call_tool("capture_image", {"device_id": "synthetic_camera"})
                assert any(c.type == "image" and c.mime_type == "image/jpeg" for c in image.content)
                state = (await dc.call_tool("get_task_status", {})).structured_content
                assert not state["running"] and not controls
                result["late_device_registration_and_native_image"] = True
                result["status_query_has_no_control_side_effect"] = True
            print("actual Codex MCP routing", flush=True)
            inventory = await backend.client.request(
                "mcpServerStatus/list", {"threadId": backend.thread_id, "detail": "toolsAndAuthOnly"}
            )
            own = [d for d in inventory["data"] if d["name"] in {"memory", "device"}]
            result["codex_inventory"] = [
                {
                    "name": d["name"],
                    "runtime_status": d.get("runtimeStatus"),
                    "tools": list(d.get("tools", {})),
                }
                for d in own
            ]
            result["platform_servers"] = [
                {"name": d["name"], "tool_count": len(d.get("tools", {}))}
                for d in inventory["data"]
                if d["name"] not in {"memory", "device"}
            ]
            assert {d["name"] for d in own} == {"memory", "device"}, result["codex_inventory"]
            assert not any(d["name"] == "personal_agent" for d in inventory["data"])
            await backend.submit(
                "这是隔离的MCP验收。必须使用 memory MCP 的 search_memory 查询“本次验收主题颜色”（scope=project），并用 device MCP 的 list_devices 和 inspect_device 查询 synthetic_temperature。只用这三个只读工具，不用shell，不写入。简短报告已查到的主题颜色、模拟温度读数，并说明读数来自模拟设备。"
            )
            async with asyncio.timeout(120):
                while backend.busy:
                    await asyncio.sleep(0.1)
            required = {("memory", "search_memory"), ("device", "list_devices"), ("device", "inspect_device")}
            assert required <= {(c["server"], c["tool"]) for c in calls if c["status"] == "completed"}, calls
            task = store.recent(1)[0]
            assert task["status"] == "completed" and "绿色" in task["result"] and "23.5" in task["result"], (
                task
            )
            result["codex_calls"] = calls
            result["actual_codex_routing"] = True
            print("resume replaces former combined registration without replay", flush=True)
            session, thread = backend.session_id, backend.thread_id
            task_count = store.db.execute("SELECT count(*) FROM tasks").fetchone()[0]
            await backend.close()
            backend = CodexBackend(isolated, store, quiet, quiet)
            request = backend.client.request

            async def former_registration(method, params, **kwargs):
                if method == "thread/resume":
                    params = {
                        **params,
                        "config": {
                            **params.get("config", {}),
                            "mcp_servers": {"personal_agent": server_config(isolated.socket)["memory"]},
                        },
                    }
                return await request(method, params, **kwargs)

            backend.client.request = former_registration
            await backend.start()
            old = await backend.client.request(
                "mcpServerStatus/list", {"threadId": thread, "detail": "toolsAndAuthOnly"}
            )
            assert any(d["name"] == "personal_agent" for d in old["data"])
            await backend.close()
            backend = CodexBackend(isolated, store, quiet, quiet)
            await backend.start()
            assert backend.session_id == session and backend.thread_id == thread
            resumed = await backend.client.request(
                "mcpServerStatus/list", {"threadId": thread, "detail": "toolsAndAuthOnly"}
            )
            names = {d["name"] for d in resumed["data"]}
            assert {"memory", "device"} <= names and "personal_agent" not in names, names
            assert store.db.execute("SELECT count(*) FROM tasks").fetchone()[0] == task_count
            result["resume_preserves_session_and_removes_old_server"] = True

            result["passed"] = True
        finally:
            await backend.close()
            if server:
                server.close()
                await server.wait_closed()
            await runtime.close()
            store.db.close()
            result["finished"] = time.time()
            (evidence / "acceptance.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
            print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.toml")
    args = parser.parse_args()
    asyncio.run(verify(Config.load(args.config)))


if __name__ == "__main__":
    main()
