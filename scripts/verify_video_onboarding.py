"""Explicit installed camera/socket/Codex acceptance; no audio or default recording."""

import argparse
import asyncio
import base64
import dataclasses
import hashlib
import io
import json
from pathlib import Path
import tempfile
import time

from PIL import Image, ImageStat

from assistant_runtime.cli import request, systemctl
from assistant_runtime.config import Config
from assistant_runtime.plugins.codex.backend import CodexBackend
from assistant_runtime.store import Store


async def snapshot(config, ident):
    started = time.monotonic()
    result = await request(config, {"command": "devices", "action": "snapshot", "device_id": ident})
    elapsed = time.monotonic() - started
    metadata = result["frame"]
    data = base64.b64decode(metadata.pop("data_base64"), validate=True)
    with Image.open(io.BytesIO(data)) as image:
        image.load()
        assert image.format == "JPEG"
        assert image.size == (metadata["width"], metadata["height"])
        deviation = sum(ImageStat.Stat(image.convert("RGB")).stddev) / 3
    assert metadata["device_id"] == ident
    assert deviation > 3, "Camera frame appears blank; inspect the saved frame"
    return {
        **metadata,
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "spatial_stddev": round(deviation, 2),
        "capture_ms": round(elapsed * 1000),
    }, data


async def gateway_command(action):
    process = await asyncio.create_subprocess_exec(
        "docker",
        "compose",
        action,
        "go2rtc",
        cwd=Path(__file__).resolve().parents[1],
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        async with asyncio.timeout(30):
            code = await process.wait()
        if code:
            raise RuntimeError(f"go2rtc gateway {action} failed (exit {code})")
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


async def verify_gateway_recovery(config, ident):
    if config.devices[ident]["adapter"] != "go2rtc":
        raise ValueError("Gateway recovery requires a go2rtc camera")
    before = await request(config, {"command": "status"})
    try:
        await gateway_command("stop")
        missing = await request(config, {"command": "devices", "action": "inspect", "device_id": ident})
        assert missing["device"]["state"] == "unavailable", missing
        try:
            await snapshot(config, ident)
        except RuntimeError:
            pass
        else:
            raise AssertionError("Stopped gateway must not return an old frame")
        healthy = await request(config, {"command": "status"})
        assert healthy["capture_recent"], "Gateway outage interrupted the microphone"
        assert healthy["room"] == before["room"]
    finally:
        await gateway_command("start")
    async with asyncio.timeout(30):
        while True:
            recovered = await request(config, {"command": "devices", "action": "inspect", "device_id": ident})
            if recovered["device"]["state"] == "available":
                break
            await asyncio.sleep(0.5)
    assert recovered["device"]["data"]["capture_verified"] is False
    metadata, _ = await snapshot(config, ident)
    after = await request(config, {"command": "status"})
    assert after["session_id"] == before["session_id"]
    assert after["room"] == before["room"]
    return {
        "outage_reported": True,
        "stale_frame_rejected": True,
        "microphone_preserved": True,
        "session_preserved": True,
        "output_preserved": True,
        "snapshot": metadata,
    }


async def verify(config, ident, save_frame, restart, codex, gateway_restart=False):
    folder = config.state_dir / "acceptance" / "video"
    folder.mkdir(parents=True, exist_ok=True)
    report = {
        "started_at": time.time(),
        "device_id": ident,
        "captures": [],
        "raw_video_saved": False,
        "scope": "local_and_codex" if codex else "local_only",
        "codex_validated": False,
    }
    backend = None
    store = None
    try:
        initial = await request(config, {"command": "status"})
        report["before"] = {
            k: initial[k] for k in ("room", "mode", "wake_count", "capture_recent", "session_id")
        }
        inventory = await request(config, {"command": "devices", "action": "list"})
        report["inventory"] = inventory
        detail = await request(config, {"command": "devices", "action": "inspect", "device_id": ident})
        assert detail["device"]["state"] == "available", detail
        assert "video.snapshot" in detail["device"]["capabilities"]
        assert detail["device"]["data"]["capturing"] is False
        report["initial_camera"] = detail
        report["adapter"] = detail["device"]["adapter"]
        for index in range(3):
            metadata, data = await snapshot(config, ident)
            report["captures"].append(metadata)
            current = await request(config, {"command": "status"})
            assert current["room"] == initial["room"]
            assert current["capture_recent"], "Camera acquisition interrupted live microphone capture"
            if save_frame and index == 0:
                image_path = folder / "preview.jpg"
                image_path.write_bytes(data)
                image_path.chmod(0o600)
                report["saved_acceptance_image"] = str(image_path)
        times = [capture["captured_at"] for capture in report["captures"]]
        assert times == sorted(set(times)), "A stale frame was reused"
        try:
            await request(config, {"command": "devices", "action": "snapshot", "device_id": "unknown_camera"})
        except RuntimeError as exc:
            report["missing_device_error"] = str(exc)
        else:
            raise AssertionError("Unknown camera should fail")
        detail = await request(config, {"command": "devices", "action": "inspect", "device_id": ident})
        before_ai_count = detail["device"]["data"]["capture_count"]
        if codex:
            with tempfile.TemporaryDirectory(prefix="camera-ai-", dir=config.state_dir) as temp:
                store = Store(Path(temp) / "acceptance.sqlite3")

                async def no_speech(*args):
                    pass

                async def no_control(*args):
                    raise RuntimeError("Acceptance does not allow audio control")

                # Keep the installed capability socket; isolate the provider thread/task records.
                readonly = dataclasses.replace(config, codex_sandbox="read-only", approval_policy="never")
                backend = CodexBackend(readonly, store, no_speech, no_control)
                calls = []
                original_get = backend.client.events.get

                async def trace():
                    event = await original_get()
                    item = event.get("params", {}).get("item", {})
                    if event.get("method") == "item/completed" and item.get("type") == "mcpToolCall":
                        content = (item.get("result") or {}).get("content", [])
                        calls.append(
                            {
                                "server": item.get("server"),
                                "tool": item.get("tool"),
                                "status": item.get("status"),
                                "arguments": item.get("arguments"),
                                "content_types": [part.get("type") for part in content],
                                "error": item.get("error"),
                            }
                        )
                    return event

                backend.client.events.get = trace
                try:
                    await backend.start()
                    await backend.submit(
                        f"用户明确要求本次摄像头看图验收。必须实际调用 device MCP 的工具："
                        f"先 list_devices，然后 inspect_device,device_id={ident}，最后 capture_image,device_id={ident}。"
                        "根据 capture_image 返回的实际图片，用中文描述最明显的物体、布局和颜色（两到三句）。"
                        "不要猜测人员身份。只能调用以上设备查询，不使用 shell、memory、音量或音箱控制，"
                        "不修改文件、不启动语音。若图片无法理解如实说明。"
                    )
                    async with asyncio.timeout(150):
                        while backend.busy:
                            await asyncio.sleep(0.25)
                    report["codex_tools"] = calls
                    report["codex_result"] = store.recent(1)
                    assert not backend.last_error, backend.last_error
                    assert any(
                        call["server"] == "device"
                        and call["tool"] == "capture_image"
                        and "image" in call["content_types"]
                        for call in calls
                    ), "Actual Codex MCP result did not contain an image block"
                    result = store.recent(1)
                    assert result and result[0]["status"] == "completed", result
                finally:
                    await backend.close()
                    backend = None
                    store.db.close()
                    store = None
        detail = await request(config, {"command": "devices", "action": "inspect", "device_id": ident})
        if codex:
            assert detail["device"]["data"]["capture_count"] > before_ai_count
            report["codex_validated"] = True
        report["after_camera"] = detail
        after = await request(config, {"command": "status"})
        assert after["room"] == initial["room"]
        report["after"] = {
            k: after[k] for k in ("room", "mode", "wake_count", "capture_recent", "session_id")
        }
        report["capture_did_not_activate_voice"] = after["wake_count"] == initial["wake_count"]
        if gateway_restart:
            report["gateway_recovery"] = await verify_gateway_recovery(config, ident)
        if restart:
            if after["task_running"]:
                raise RuntimeError("Do not restart while a foreground task is running")
            systemctl("restart", config.unit_name)
            async with asyncio.timeout(60):
                while True:
                    try:
                        healthy = await request(config, {"command": "status"})
                        recovered = await request(
                            config, {"command": "devices", "action": "inspect", "device_id": ident}
                        )
                        if healthy["capture_recent"] and recovered["device"]["state"] == "available":
                            break
                    except (OSError, RuntimeError):
                        pass
                    await asyncio.sleep(0.5)
            assert healthy["session_id"] == initial["session_id"]
            assert healthy["room"] == initial["room"]
            recovered_frame, _ = await snapshot(config, ident)
            report["restart"] = {
                "session_preserved": True,
                "output_preserved": True,
                "capture_recent": healthy["capture_recent"],
                "snapshot": recovered_frame,
            }
        report["passed"] = True
    except BaseException as exc:
        report["passed"] = False
        report["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        if backend:
            await backend.close()
        if store:
            store.db.close()
        report["finished_at"] = time.time()
        evidence = folder / "real-devices.json"
        evidence.write_text(json.dumps(report, ensure_ascii=False, indent=2))
        evidence.chmod(0o600)
        print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.toml")
    parser.add_argument("--device", required=True)
    parser.add_argument(
        "--save-frame", action="store_true", help="Explicitly retain one private acceptance JPEG"
    )
    parser.add_argument(
        "--restart", action="store_true", help="Verify installed service restart after captures"
    )
    parser.add_argument(
        "--restart-gateway",
        action="store_true",
        help="Verify a real go2rtc outage, stale-frame rejection and recovery",
    )
    parser.add_argument(
        "--codex",
        action="store_true",
        help="Explicitly send a test image to Codex/OpenAI for visual acceptance",
    )
    args = parser.parse_args()
    asyncio.run(
        verify(
            Config.load(args.config),
            args.device,
            args.save_frame,
            args.restart,
            args.codex,
            args.restart_gateway,
        )
    )


if __name__ == "__main__":
    main()
