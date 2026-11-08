"""Explicit isolated PP hardware rig: real PA/Pi, synthetic LLM, optional real Ali speech.

Run from the source checkout. Requires an operator-provided exact device ID and a
private empty root, or --resume for an explicitly marked rig. Only this device can pair. A temporary LAN port serves this rig,
never the production 8047 entry. Microphone data is counted, not retained. TTS is a
short low-amplitude tone by default. --real-speech-key-file explicitly enables Ali
under the existing original acceptance ledger and cumulative CNY10 allowance.
Software clients cannot establish physical button, display or acoustic acceptance.
"""

import argparse
import asyncio
import json
import math
import os
from pathlib import Path
import signal
import sqlite3
import struct
import sys
import time
import uuid

from aiohttp import ClientSession, WSMsgType, WSServerHandshakeError, web

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from pi_gateway import FakeResponsesGateway, sse_text_reply, sse_tool_call  # noqa: E402
from test_pi_skeleton import configuration  # noqa: E402
from assistant_runtime.app.config import Config  # noqa: E402
from assistant_runtime.app.host import AssistantService  # noqa: E402
from assistant_runtime.api.ai_usage import UsageError  # noqa: E402
from assistant_runtime.core.ai_usage.service import UsageService  # noqa: E402
from assistant_runtime.core.ai_usage.settings import UsageSettings  # noqa: E402


def speech_cost(path):
    """Operator-only accounting; count unresolved reservations, never clear them."""
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as db:
        rows = db.execute(
            "SELECT state,cost,reserved_cost FROM calls WHERE provider='aliyun_speech'"
        ).fetchall()
    return sum(
        (reserved or 0) if state in {"unknown", "submitted"} else (cost or 0)
        for state, cost, reserved in rows
    )


class SpeechAllowance:
    """Reuse the original speech ledger and original CNY10 cap, not a fresh test allowance."""

    def __init__(self, directory, other):
        self.directory, self.other = directory.resolve(), other.resolve()
        self.ledger = self.directory / "usage.sqlite3"
        if not self.ledger.is_file() or not self.other.is_file() or self.ledger == self.other:
            raise ValueError("Real speech requires two distinct existing acceptance/production ledgers")
        self.lock = asyncio.Lock()
        self.usage = UsageService(self.directory, self.settings())

    def settings(self):
        remaining = 10 - speech_cost(self.other)
        if remaining <= 0 or speech_cost(self.ledger) >= remaining:
            raise UsageError("passport_original_speech_allowance_exhausted")
        return UsageSettings.parse(
            {
                "rules": [
                    {
                        "id": "pp-original-10-cny",
                        "scope": "global",
                        "match": {"provider": "aliyun_speech"},
                        "window_seconds": 7776000,
                        "limits": {"max_cost_cny": remaining},
                    }
                ],
                "prices": {
                    "asr": {
                        "provider": "aliyun_speech",
                        "model": "fun-asr-realtime",
                        "audio_cny_per_second": 0.001,
                    },
                    "tts": {
                        "provider": "aliyun_speech",
                        "model": "cosyvoice-v3-flash",
                        "text_cny_per_character": 0.001,
                    },
                },
            }
        )

    async def admit(self, scope, **dimensions):
        async with self.lock:
            # Refresh the other ledger before every dispatch, not only on process startup.
            settings = self.settings()
            await self.usage.call("configure", settings)
            self.usage.settings = settings
            return await self.usage.admit(scope, **dimensions)

    async def settle(self, *args, **kwargs):
        return await self.usage.settle(*args, **kwargs)


async def run(args):
    root = args.root.resolve()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    marker = root / "hardware-rig.json"
    expected = {"kind": "pp-synthetic-hardware", "version": 1, "device_id": args.device_id}
    model_port = 0
    if args.resume:
        saved = json.loads(marker.read_text()) if marker.is_file() else {}
        model_port = saved.pop("model_port", None)
        if saved != expected or type(model_port) is not int or not 1024 <= model_port <= 65535:
            raise ValueError("Resume requires this exact device's marked synthetic rig")
    elif any(root.iterdir()):
        raise ValueError("Use an empty private rig directory; production state is never reused")
    root.chmod(0o700)
    env = root / ".env"
    synthetic_credentials = "TEST_KEY=synthetic-key\nDASHSCOPE_API_KEY=synthetic-key\n"
    if args.resume:
        if env.read_text() != synthetic_credentials:
            raise ValueError("Synthetic rig credentials differ; refusing to resume")
    else:
        fd = os.open(env, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as file:
            file.write(synthetic_credentials)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for event in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(event, stop.set)
    received_bytes = 0
    speech_app = web.Application(client_max_size=16384)

    async def speech(request):
        nonlocal received_bytes
        ws = web.WebSocketResponse(max_msg_size=65536)
        await ws.prepare(request)
        task, ident, count, last_partial = "", "", 0, 0
        async for frame in ws:
            if frame.type == WSMsgType.BINARY:
                count += len(frame.data)
                received_bytes += len(frame.data)
                if count - last_partial >= 16000:
                    last_partial = count
                    await ws.send_json(
                        {
                            "header": {"task_id": ident, "event": "result-generated"},
                            "payload": {
                                "output": {
                                    "sentence": {
                                        "text": f"合成转写，已收到 {count / 32000:.1f} 秒音频",
                                        "sentence_end": False,
                                    }
                                }
                            },
                        }
                    )
                continue
            if frame.type != WSMsgType.TEXT:
                break
            command = json.loads(frame.data)
            ident = command["header"]["task_id"]
            action = command["header"]["action"]
            if action == "run-task":
                task = command["payload"]["task"]
                await ws.send_json({"header": {"task_id": ident, "event": "task-started"}})
            elif action == "finish-task":
                if task == "asr":
                    await ws.send_json(
                        {
                            "header": {"task_id": ident, "event": "result-generated"},
                            "payload": {
                                "output": {
                                    "sentence": {
                                        "text": f"这是本地硬件测试，收到 {count / 32000:.1f} 秒音频。",
                                        "sentence_end": True,
                                        "begin_time": 0,
                                    }
                                }
                            },
                        }
                    )
                else:
                    samples = int(24000 * args.tone_seconds)
                    for offset in range(0, samples, 1024):
                        pcm = b"".join(
                            struct.pack("<h", int(1200 * math.sin(2 * math.pi * 660 * i / 24000)))
                            for i in range(offset, min(offset + 1024, samples))
                        )
                        await ws.send_bytes(pcm)
                await ws.send_json({"header": {"task_id": ident, "event": "task-finished"}})
        return ws

    speech_app.router.add_get("/speech", speech)
    speech_runner = web.AppRunner(speech_app)
    proxy_runner = allowance = None
    running = host = None
    run_id = uuid.uuid4().hex[:12]
    image_step = 1 if args.image_fixture else 0
    task_pending = 1 if args.task_fixture else 0
    image_path = root / "color-check.png"
    if args.image_fixture:
        from PIL import Image, ImageDraw

        image = Image.new("RGB", (160, 96), "white")
        draw = ImageDraw.Draw(image)
        for i, color in enumerate(("red", "lime", "blue", "yellow", "cyan", "magenta")):
            x, y = (i % 3) * 53, (i // 3) * 48
            draw.rectangle((x, y, x + 52, y + 47), fill=color)
        draw.rectangle((0, 0, 159, 95), outline="white", width=2)
        draw.rectangle((61, 31, 98, 64), fill="black", outline="white", width=2)
        image.save(image_path)
        image_path.chmod(0o600)

    def reply(step, body):
        nonlocal image_step, task_pending
        if image_step == 1:
            image_step = 2
            return sse_tool_call(f"image-{run_id}-{step}", "read", {"path": str(image_path)})
        if task_pending and image_step == 0:
            if task_pending == 1:
                task_pending = 2
                return sse_tool_call(
                    f"discover-task-{run_id}-{step}",
                    "tool_search",
                    {"query": "mcp__tasks__create_task", "limit": 1},
                )
            task_pending = 0
            return sse_tool_call(
                f"task-{run_id}-{step}",
                "mcp__tasks__create_task",
                {
                    "request_id": str(uuid.uuid4()),
                    "title": "PP 隔离验收任务",
                    "prompt": "只回复：隔离任务已完成。不得操作设备、读取个人文件或创建子任务。",
                    "execution": {
                        "reasoning_effort": "low",
                        "reason": "仅生成合成文字，核对执行完成及本PP通知归属；无外部动作。",
                    },
                    "notify_voice": True,
                },
            )
        text = "收到，收音与回复链路已完成。这是本地合成验收，不会操作设备或创建真实任务。"
        if args.task_fixture:
            text = "这是隔离验收回复，未操作真实设备。后台结果按本 PP 的会话归属投递。"
        if image_step == 2:
            image_step = 0
            text = "这是合成色卡。图片用于检查颜色、完整显示和翻页，不是相机照片。"
        elif args.long_reply:
            text += (
                "这是长回复翻页测试。你可以先看文字逐段出现，播放时用上下键调整音量，按一下OK结束本条声音。"
                "停止声音后，上下键恢复浏览正文，翻到边界会继续查看前后消息。"
                "历史只保留有限索引和文字页，继续往前翻时在线查询，不把整段会话留在设备内存里。"
                "这段内容是合成的验收文字，不会创建提醒、安排计划或控制任何真实设备。"
                "你可以双击OK切换引导与排队，切换后再正常说话，检查下一次输入是否仍然顺畅。"
            )
        events = sse_text_reply(f"hardware-{run_id}-{step}", text)
        for event in events:
            if event.get("item", {}).get("type") == "message":
                event["item"]["phase"] = "final_answer"

        def streaming():
            for event in events:
                if event["type"] == "response.output_text.delta":
                    for offset in range(0, len(event["delta"]), 4):
                        yield {**event, "delta": event["delta"][offset : offset + 4]}
                        time.sleep(0.08)
                else:
                    yield event

        return streaming()

    gateway = FakeResponsesGateway(reply, port=model_port)
    if not args.resume:
        fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as file:
            json.dump({**expected, "model_port": gateway.port}, file)
    client = None
    try:
        await speech_runner.setup()
        speech_site = web.TCPSite(speech_runner, "127.0.0.1", 0)
        await speech_site.start()
        speech_port = speech_site._server.sockets[0].getsockname()[1]
        config = Config(
            root=root,
            workspace=str(root),
            main_model="model-a",
            main_reasoning_effort="auto",
            model_api=configuration(gateway.base_url),
            plugins=[
                "ai_usage",
                "models",
                "workfiles",
                "pi",
                "execution",
                "conversation",
                "cli",
                "tasks",
                "notifications",
                "speech_io",
                "aliyun_speech",
                "ai_passport",
            ],
            ai_passport={
                "keep_online": True,
                **(
                    {"devices": {args.device_id: {"server_url": args.device_server_url}}}
                    if args.device_server_url
                    else {}
                ),
            },
        )
        host = AssistantService(config)
        running = asyncio.create_task(host.run())
        ready = asyncio.create_task(host.runtime.ready.wait())
        try:
            done, _ = await asyncio.wait([ready, running], timeout=30, return_when=asyncio.FIRST_COMPLETED)
            if running in done:
                await running  # Preserve the actual startup error, e.g. an overlong IPC path.
                raise RuntimeError("Isolated service stopped before readiness")
            if ready not in done:
                raise TimeoutError("Isolated hardware rig did not become ready within 30 seconds")
        finally:
            ready.cancel()
            await asyncio.gather(ready, return_exceptions=True)
        for event in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(event, stop.set)
        if host.runtime.health()["ai_passport"]["state"] != "ready":
            raise RuntimeError("Isolated PP plugin did not become ready")
        provider = host.runtime.plugins["aliyun_speech"].instance
        if args.real_speech_key_file:
            from dotenv import dotenv_values

            key = dotenv_values(args.real_speech_key_file).get("DASHSCOPE_API_KEY")
            if not key:
                raise ValueError("DASHSCOPE_API_KEY is missing in the specified private file")
            allowance = SpeechAllowance(args.speech_ledger, args.other_speech_ledger)
            await allowance.usage.start()
            provider.key, provider.usage = key, allowance
            print("Real Ali speech enabled under the original cumulative CNY10 allowance", flush=True)
        else:
            provider.settings = {**provider.settings, "endpoint": f"ws://127.0.0.1:{speech_port}/speech"}
        frontend = host.http_frontends["ai_passport"]
        endpoint = frontend.endpoint
        base = f"http://{endpoint['host']}:{endpoint['port']}"
        client = ClientSession()
        app = web.Application(client_max_size=16384)

        def headers(request):
            return {
                "X-PA-Frontend-Key": endpoint["key"],
                **{k: request.headers[k] for k in ("Authorization", "X-Device-Id") if k in request.headers},
            }

        async def pair(request):
            value = await request.json()
            if value.get("device_id") != args.device_id:
                raise web.HTTPForbidden(text="This rig accepts only its designated device")
            async with client.post(base + "/pair", json=value, headers=headers(request)) as response:
                data, status = await response.json(), response.status
            if status == 200 and data.get("code"):
                await frontend.management("pair", {"name": "Hardware Acceptance PP", "code": data["code"]})
                print("designated hardware paired (isolated synthetic instance)", flush=True)
            return web.json_response(data, status=status)

        async def bridge(request):
            if (
                request.headers.get("X-Device-Id") != args.device_id
                or "Origin" in request.headers
                or "Cookie" in request.headers
            ):
                raise web.HTTPForbidden()
            try:
                upstream = await client.ws_connect(
                    base + "/stream", headers=headers(request), max_msg_size=16384, heartbeat=15
                )
            except WSServerHandshakeError as exc:
                return web.Response(
                    status=exc.status if 400 <= exc.status < 600 else 502,
                    text="terminal_connection_rejected",
                )
            async with upstream:
                downstream = web.WebSocketResponse(max_msg_size=16384, heartbeat=15)
                await downstream.prepare(request)

                async def pump(source, destination):
                    async for frame in source:
                        if frame.type == WSMsgType.BINARY:
                            await destination.send_bytes(frame.data)
                        elif frame.type == WSMsgType.TEXT:
                            await destination.send_str(frame.data)
                        else:
                            break

                tasks = [
                    asyncio.create_task(pump(upstream, downstream)),
                    asyncio.create_task(pump(downstream, upstream)),
                ]
                try:
                    await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                    await downstream.close()
                return downstream

        app.router.add_post("/entrances/ai_passport/pair", pair)
        app.router.add_get("/entrances/ai_passport/stream", bridge)
        proxy_runner = web.AppRunner(app, access_log=None)
        await proxy_runner.setup()
        await web.TCPSite(proxy_runner, args.bind, args.port).start()
        print(
            f"Hardware rig ready: http://{args.bind}:{args.port} (isolated, synthetic LLM)",
            flush=True,
        )
        if not allowance:
            print("ASR is a placeholder; TTS is a short tone. No paid calls.", flush=True)
        waiting = asyncio.create_task(stop.wait())
        try:
            await asyncio.wait([waiting, running], return_when=asyncio.FIRST_COMPLETED)
            if running.done():
                await running
                if not stop.is_set():
                    raise RuntimeError("Isolated business service stopped unexpectedly")
        finally:
            waiting.cancel()
            await asyncio.gather(waiting, return_exceptions=True)
    finally:
        if host:
            # Close the owned upstream WebSockets while the HTTP runner drains;
            # otherwise its healthy bridge can keep shutdown waiting indefinitely.
            host.stop_event.set()
        if proxy_runner:
            await proxy_runner.cleanup()
        if client:
            await client.close()
        if running:
            try:
                await asyncio.wait_for(running, 15)
            except BaseException:
                running.cancel()
                await asyncio.gather(running, return_exceptions=True)
        await speech_runner.cleanup()
        await gateway.aclose()
        if allowance:
            await allowance.usage.close()
            total = speech_cost(allowance.ledger) + speech_cost(allowance.other)
            print(f"Cumulative conservative speech occupancy: CNY {total:.3f} / 10", flush=True)
        if allowance:
            print("Real speech settled through the original ledger; no raw audio saved", flush=True)
        else:
            print(f"Microphone PCM received: {received_bytes} bytes; no raw audio saved", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--device-id", required=True)
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18047)
    parser.add_argument("--resume", action="store_true", help="Resume this exact marked synthetic instance")
    parser.add_argument(
        "--device-server-url",
        help="Explicit operator handoff: send this device a new PA root through normal cloud settings",
    )
    parser.add_argument(
        "--tone-seconds", type=float, default=0.4, help="Synthetic tone duration, 0.2 to 4 seconds"
    )
    parser.add_argument(
        "--long-reply", action="store_true", help="Stream a long synthetic reply for pagination"
    )
    parser.add_argument(
        "--task-fixture", action="store_true", help="Create one isolated real-Pi background task"
    )
    parser.add_argument(
        "--real-speech-key-file", type=Path, help="Explicit opt-in: private .env containing Ali speech key"
    )
    parser.add_argument("--speech-ledger", type=Path, help="Original acceptance ledger directory")
    parser.add_argument("--other-speech-ledger", type=Path, help="Existing production usage.sqlite3")
    parser.add_argument(
        "--image-fixture",
        action="store_true",
        help="First input uses real Pi read on a synthetic color chart",
    )
    args = parser.parse_args()
    if len(args.device_id) != 32 or any(c not in "0123456789abcdef" for c in args.device_id):
        parser.error("Expected the exact device_id from the read-only pp.status command")
    if not math.isfinite(args.tone_seconds) or not 0.2 <= args.tone_seconds <= 4:
        parser.error("Tone duration must be 0.2 to 4 seconds")
    if sum(
        bool(v) for v in (args.real_speech_key_file, args.speech_ledger, args.other_speech_ledger)
    ) not in (0, 3):
        parser.error("Real speech requires the key file and both original ledger paths")
    asyncio.run(run(args))
