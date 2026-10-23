"""Repeat sensor HTTP/MCP/AI acceptance; real readback or explicitly synthetic source."""

import argparse
import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
import json
from pathlib import Path
import re
import socket
import sys
import tempfile
import time

from aiohttp import ClientSession
from fastmcp import Client

from assistant_runtime.cli import request, systemctl
from assistant_runtime.config import Config
from assistant_runtime.devices.directory import DeviceDirectory, DevicesPlugin
from assistant_runtime.kernel.contracts import DeviceQuery
from assistant_runtime.kernel.runtime import Runtime
from assistant_runtime.mcp.client import LocalRuntimeClient
from assistant_runtime.plugins.apple_home.plugin import AppleHomePlugin
from assistant_runtime.plugins.pi.backend import PiBackend
from assistant_runtime.plugins.ai_usage.service import UsageService
from assistant_runtime.plugins.ai_usage.settings import UsageSettings
from assistant_runtime.store import Store


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def check_readings(device):
    assert device["kind"] == "sensor", device
    assert device["readings"], "No sensor measurements received"
    for metric, reading in device["readings"].items():
        assert reading["fresh"] and reading["status"] == "valid", f"{metric} is not current: {reading}"
        assert reading["value"] is not None and reading["observed_at"] is not None
    return device


@asynccontextmanager
async def readonly_socket(config, query):
    """An isolated acceptance provider can only query devices, never control them."""

    async def connection(reader, writer):
        try:
            payload = json.loads(await asyncio.wait_for(reader.readline(), 5))
            if payload.get("command") != "devices" or payload.get("action") not in {"list", "inspect"}:
                raise ValueError("Sensor acceptance allows device queries only")
            data = await query(DeviceQuery(payload["action"], payload.get("device_id")))
            result = {"ok": True, "result": data}
        except Exception as exc:
            result = {"ok": False, "error": str(exc)[:240]}
        try:
            writer.write((json.dumps(result, ensure_ascii=False) + "\n").encode())
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_unix_server(connection, path=config.socket, limit=8192)
    config.socket.chmod(0o600)
    try:
        yield
    finally:
        server.close()
        await server.wait_closed()
        config.socket.unlink(missing_ok=True)


async def pipeline(config, ident, query, execution, report):
    with tempfile.TemporaryDirectory(prefix="sensor-query-") as folder:
        isolated = replace(config, root=Path(folder), workspace=folder, memory_auto_capture=False)
        isolated.state_dir.mkdir(parents=True, mode=0o700)
        store = Store(isolated.state_dir / "assistant.sqlite3")
        backend = None
        usage = None
        try:
            async with readonly_socket(isolated, query):
                async with Client(
                    {
                        "mcpServers": {
                            "device": {
                                "command": sys.executable,
                                "args": [
                                    "-m",
                                    "assistant_runtime.mcp",
                                    "device",
                                    "--socket",
                                    str(isolated.socket),
                                ],
                            }
                        }
                    }
                ) as mcp:
                    devices = (await mcp.call_tool("list_devices", {})).structured_content["devices"]
                    descriptor = next(d for d in devices if d["id"] == ident)
                    report["physical_endpoints"] = [
                        d["id"] for d in devices if d["physical_id"] == descriptor["physical_id"]
                    ]
                    report["mcp_readback"] = check_readings(
                        (await mcp.call_tool("inspect_device", {"device_id": ident})).structured_content[
                            "device"
                        ]
                    )
                if execution:
                    from assistant_runtime.plugins.models.gateway import gateway_credential
                    from assistant_runtime.plugins.models.settings import ModelSettings

                    settings = ModelSettings.parse(config.model_api)
                    private_key = Path(folder) / ".env"
                    private_key.write_text(
                        settings.gateway_key_env + "=" + gateway_credential(config.root, settings) + "\n"
                    )
                    private_key.chmod(0o600)

                    async def quiet(*args):
                        pass

                    usage = UsageService(Path(folder) / "ai-usage", UsageSettings(max_requests=12))
                    await usage.start()
                    backend = PiBackend(
                        isolated, store, quiet, quiet, usage=usage, runtime_dir=Path(folder) / "executions"
                    )
                    calls, unexpected = [], []
                    original = backend.client.events.get

                    async def trace():
                        event = await original()
                        item = event.get("data", {}).get("item", {})
                        if event.get("kind") == "activity.completed":
                            if item.get("type") == "mcp":
                                calls.append(
                                    {
                                        k: item.get(k)
                                        for k in ("server", "tool", "arguments", "status", "result", "error")
                                    }
                                )
                            elif item.get("type") in {"command", "dynamicToolCall", "file"}:
                                unexpected.append(item.get("type"))
                        return event

                    backend.client.events.get = trace
                    await backend.start()
                    await backend.submit(
                        f"用户要求本次传感器只读验收。必须先调用 device MCP 的 list_devices，再调用 inspect_device(device_id={ident})。"
                        "只用这两个工具，不用shell、memory、图像、音量、音箱或其他控制，不启动语音。"
                        "判断每项读数是否为当前有效值，不用心跳时间代替采样时间。"
                        '最后只返回JSON对象：{"device_id":"端点ID","readings":{"指标名":{"value":数值,"unit":"单位","fresh":true}}}。'
                        "若没有当前有效值，明确说明不可用，不编造。"
                    )
                    async with asyncio.timeout(150):
                        while backend.busy:
                            await asyncio.sleep(0.2)
                    assert not backend.last_error, backend.last_error
                    assert not unexpected, unexpected
                    assert calls and all(
                        c["server"] == "device" and c["tool"] in {"list_devices", "inspect_device"}
                        for c in calls
                    ), calls
                    assert any(
                        c["tool"] == "inspect_device"
                        and c["arguments"].get("device_id") == ident
                        and c["status"] == "completed"
                        for c in calls
                    ), calls
                    result = store.recent(1)[0]
                    assert result["status"] == "completed", result
                    parsed = json.loads(re.search(r"\{.*\}", result["result"], re.S).group())
                    assert parsed["device_id"] == ident
                    inspected = next(c for c in reversed(calls) if c["tool"] == "inspect_device")
                    actual = check_readings(inspected["result"]["structuredContent"]["device"])["readings"]
                    assert set(parsed["readings"]) == set(actual)
                    for metric, reading in parsed["readings"].items():
                        assert reading["fresh"] and type(reading["value"]) in (int, float)
                        assert reading["unit"] == actual[metric]["unit"]
                        assert reading["value"] == actual[metric]["value"], (
                            "Model answer did not match its actual measurement"
                        )
                    report["execution_calls"], report["execution_answer"] = calls, parsed
        finally:
            try:
                if backend:
                    await backend.close()
            finally:
                if usage:
                    await usage.close()
                store.db.close()


async def verify(config, ident, execution, restart, synthetic):
    evidence = config.state_dir / "acceptance" / "environment"
    evidence.mkdir(parents=True, exist_ok=True)
    report = {"passed": False, "synthetic": synthetic, "started_at": time.time(), "device_id": ident}
    try:
        if synthetic:
            with tempfile.TemporaryDirectory(prefix="sensor-source-", dir=config.state_dir) as folder:
                source = replace(
                    config,
                    root=Path(folder),
                    workspace=folder,
                    plugins=["apple_home"],
                    apple_home={"port": free_port(), "hap_port": free_port()},
                    devices={
                        ident: {
                            "adapter": "apple_home",
                            "kind": "sensor",
                            "label": "模拟环境传感器",
                            "physical_id": "synthetic_environment",
                            "max_age_seconds": 180,
                            "connection": {"metrics": ["temperature", "humidity"]},
                        }
                    },
                )
                store = Store(source.state_dir / "assistant.sqlite3")
                directory = DeviceDirectory(source.devices)
                plugin = AppleHomePlugin(source, store, directory)
                runtime = Runtime(store)
                runtime.add(plugin)
                runtime.add(DevicesPlugin(directory))
                await runtime.start()
                try:
                    async with asyncio.timeout(5):
                        while not plugin.receiver_ready or not plugin.bridge or not plugin.bridge.started:
                            await asyncio.sleep(0.02)
                    async with ClientSession() as http:
                        async with http.post(
                            f"http://127.0.0.1:{source.apple_home['port']}/readings",
                            headers={"Authorization": "Bearer " + plugin.token},
                            json={
                                "device_id": ident,
                                "observed_at": time.time() - 1,
                                "temperature": "75.2 °F",
                                "humidity": "46%",
                            },
                        ) as response:
                            assert response.status == 200 and (await response.json())["accepted"]
                    await pipeline(source, ident, directory.handle, execution, report)
                    assert store.max_transcript() == 0 and not store.recent()
                    report["sensor_update_created_no_task_or_transcript"] = True
                finally:
                    await runtime.close()
                    store.db.close()
        else:
            initial = await request(config, {"command": "status"})
            query = LocalRuntimeClient(config.socket).invoke
            await pipeline(config, ident, query, execution, report)
            after = await request(config, {"command": "status"})
            assert after["voice_io"] == initial["voice_io"]
            assert after["session_id"] == initial["session_id"]
            assert after["capture_recent"], "Microphone stopped during sensor readback"
            report["preserved"] = {
                "voice_io": after["voice_io"],
                "session_id": after["session_id"],
                "voice_wakes_unchanged": after["wake_count"] == initial["wake_count"],
                "microphone_active": True,
            }
            if restart:
                if after["task_running"]:
                    raise RuntimeError("Do not restart while a foreground task is running")
                before = report["mcp_readback"]["readings"]
                systemctl("restart", config.unit_name)
                async with asyncio.timeout(60):
                    while True:
                        try:
                            ready = await request(config, {"command": "status"})
                            if (
                                ready["capture_recent"]
                                and ready["plugins"].get("apple_home", {}).get("state") == "ready"
                            ):
                                break
                        except (OSError, RuntimeError):
                            pass
                        await asyncio.sleep(0.5)
                detail = (await query(DeviceQuery("inspect", ident)))["device"]
                for metric, reading in before.items():
                    assert detail["readings"][metric]["observed_at"] >= reading["observed_at"]
                assert (
                    ready["session_id"] == initial["session_id"] and ready["voice_io"] == initial["voice_io"]
                )
                report["restart_readback"] = detail
        report["passed"] = True
    except BaseException as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        report["finished_at"] = time.time()
        path = evidence / ("synthetic.json" if synthetic else "real-home.json")
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2))
        path.chmod(0o600)
        print(
            json.dumps(
                {"passed": report["passed"], "synthetic": synthetic, "evidence": str(path)},
                ensure_ascii=False,
            ),
            flush=True,
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config/config.toml")
    parser.add_argument("--device", required=True)
    parser.add_argument(
        "--execution", action="store_true", help="Verify actual model calls in an isolated read-only thread"
    )
    parser.add_argument(
        "--restart", action="store_true", help="Verify installed-service recovery after real readback"
    )
    parser.add_argument(
        "--synthetic", action="store_true", help="Isolated synthetic HTTP source; not real HomePod success"
    )
    args = parser.parse_args()
    if args.synthetic and args.restart:
        parser.error("Synthetic acceptance never restarts the installed service")
    asyncio.run(verify(Config.load(args.config), args.device, args.execution, args.restart, args.synthetic))


if __name__ == "__main__":
    main()
