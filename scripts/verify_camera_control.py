"""Explicit installed pan/tilt + MCP acceptance, without sending images to a model."""

import argparse
import asyncio
import dataclasses
import json
from pathlib import Path
import tempfile
import time

from fastmcp import Client
from fastmcp.exceptions import ToolError

from assistant_runtime.cli import request
from assistant_runtime.config import Config
from assistant_runtime.mcp.client import LocalRuntimeClient
from assistant_runtime.mcp.device import create_server
from assistant_runtime.plugins.codex.backend import CodexBackend
from assistant_runtime.store import Store
from verify_video_onboarding import snapshot


async def verify(config, device, fixed, codex):
    folder = config.state_dir / "acceptance" / "camera-control"
    folder.mkdir(parents=True, exist_ok=True)
    report = {
        "device_id": device,
        "fixed_device_id": fixed,
        "started_at": time.time(),
        "commands": [],
        "images_sent_to_model": False,
    }
    try:
        before = await request(config, {"command": "status"})
        async with Client(create_server(LocalRuntimeClient(config.socket))) as client:
            listing = (await client.call_tool("list_devices", {})).structured_content
            devices = {d["id"]: d for d in listing["devices"]}
            assert "camera.pan_tilt" in devices[device]["capabilities"]
            report["inventory"] = listing
            detail = (await client.call_tool("inspect_device", {"device_id": device})).structured_content
            assert detail["device"]["state"] == "available"
            assert detail["device"]["data"]["pan_tilt"]["position_feedback"] is False
            if fixed:
                assert "camera.pan_tilt" not in devices[fixed]["capabilities"]
                try:
                    await client.call_tool("move_camera", {"device_id": fixed, "direction": "left"})
                except ToolError:
                    report["fixed_move_rejected"] = True
                else:
                    raise AssertionError("Fixed camera accepted movement")
            report["before_frame"], _ = await snapshot(config, device)
            for direction in ("left", "right"):
                started = time.monotonic()
                result = (
                    await client.call_tool(
                        "move_camera", {"device_id": device, "direction": direction, "duration_ms": 80}
                    )
                ).structured_content
                assert result["command_sent"] and result["stop_sent"] and not result["position_verified"]
                report["commands"].append(
                    {**result, "elapsed_ms": round((time.monotonic() - started) * 1000)}
                )
                stop = (await client.call_tool("stop_camera", {"device_id": device})).structured_content
                assert stop["stop_sent"]
                report[direction + "_frame"], _ = await snapshot(config, device)
        if codex:
            with tempfile.TemporaryDirectory(prefix="camera-control-ai-", dir=config.state_dir) as temp:
                store = Store(Path(temp) / "acceptance.sqlite3")

                async def no_speech(*args):
                    pass

                async def no_control(*args):
                    raise RuntimeError("Acceptance cannot control audio")

                backend = CodexBackend(
                    dataclasses.replace(config, codex_sandbox="read-only", approval_policy="never"),
                    store,
                    no_speech,
                    no_control,
                )
                calls = []
                original_get = backend.client.events.get

                async def trace():
                    event = await original_get()
                    item = event.get("params", {}).get("item", {})
                    if event.get("method") == "item/completed" and item.get("type") == "mcpToolCall":
                        calls.append(
                            {k: item.get(k) for k in ("server", "tool", "arguments", "status", "error")}
                        )
                    return event

                backend.client.events.get = trace
                try:
                    await backend.start()
                    label = config.devices[device]["label"]
                    await backend.submit(
                        f"用户明确要求本次摄像头转向验收。先实际调用 device MCP 的 list_devices，再 inspect_device，"
                        f"根据能力找出“{label}”的可转向镜头。必须调用 move_camera 向左移动80毫秒，stop_camera，"
                        "再 move_camera 向右移动80毫秒，stop_camera。只执行这两个短步；每次仅调用一次。"
                        "仅调用以上设备工具，不使用 shell、memory、图像抓取、音量、音箱控制或语音。"
                        "最后说明哪个镜头可转向，以及固定镜头为什么不可转。没有位置反馈不要声称精确回原位。"
                    )
                    async with asyncio.timeout(150):
                        while backend.busy:
                            await asyncio.sleep(0.25)
                    assert not backend.last_error, backend.last_error
                    report["codex_tools"] = calls
                    report["codex_result"] = store.recent(1)
                    movement = [call for call in calls if call["tool"] == "move_camera"]
                    assert len(movement) == 2 and all(call["status"] == "completed" for call in movement)
                    arguments = [
                        json.loads(call["arguments"])
                        if isinstance(call["arguments"], str)
                        else call["arguments"]
                        for call in movement
                    ]
                    assert [a["direction"] for a in arguments] == ["left", "right"]
                    assert all(a["device_id"] == device and a.get("duration_ms", 80) == 80 for a in arguments)
                    assert not any(call["tool"] == "capture_image" for call in calls)
                    assert store.recent(1)[0]["status"] == "completed"
                    report["codex_validated"] = True
                finally:
                    await backend.close()
                    store.db.close()
        after = await request(config, {"command": "status"})
        assert after["session_id"] == before["session_id"] and after["voice_io"] == before["voice_io"]
        assert after["capture_recent"] and after["wake_count"] == before["wake_count"]
        report.update(
            passed=True, microphone_preserved=True, voice_not_activated=True, session_preserved=True
        )
    except BaseException as exc:
        report.update(passed=False, error=f"{type(exc).__name__}: {exc}")
        raise
    finally:
        report["finished_at"] = time.time()
        path = folder / f"{device}.json"
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2))
        path.chmod(0o600)
        print(
            json.dumps(
                {
                    k: report.get(k)
                    for k in (
                        "passed",
                        "codex_validated",
                        "fixed_move_rejected",
                        "microphone_preserved",
                        "voice_not_activated",
                        "error",
                    )
                },
                ensure_ascii=False,
            ),
            flush=True,
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config/config.toml")
    parser.add_argument("--device", required=True)
    parser.add_argument("--fixed-device")
    parser.add_argument(
        "--codex", action="store_true", help="Exercise real Codex control tools; no image upload"
    )
    args = parser.parse_args()
    asyncio.run(verify(Config.load(args.config), args.device, args.fixed_device, args.codex))
