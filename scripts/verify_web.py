"""Repeatable isolated subscription/browser acceptance; never uses room devices or user history."""

import argparse
import asyncio
from dataclasses import replace
import json
from pathlib import Path
import socket
import tempfile
import time
from types import SimpleNamespace
import uuid

import soxr
import numpy as np
from PIL import Image, ImageDraw
from playwright.async_api import async_playwright

from assistant_runtime.config import Config
from assistant_runtime.store import Store
from assistant_runtime.kernel.runtime import Context, Runtime, Manifest
from assistant_runtime.plugins.codex.backend import CodexBackend
from assistant_runtime.plugins.codex.native import NativeVoice
from assistant_runtime.conversation.service import Conversation
from assistant_runtime.plugins.web.plugin import WebPlugin


class AcceptanceHost:
    def __init__(self, config):
        self.config = config
        self.store = Store(config.state_dir / "assistant.sqlite3")
        self.player = SimpleNamespace(playing=False)
        self.memory = None
        self.mode = "standby"
        self.transcripts = []
        self.voiced_frames = 0
        self.mute = False
        self.capture = None
        self.backend = CodexBackend(config, self.store, self.speak, self.tool_control)
        self.voice = NativeVoice(config, self.backend, self.audio, self.event)
        self.backend.on_realtime = self.voice.notification
        self.conversation = Conversation(self)
        self.runtime = SimpleNamespace(invoke=self.control)
        self.owned_runtime = Runtime(self.store)
        self.owned_runtime.plugins["acceptance"] = SimpleNamespace(manifest=Manifest("acceptance"))
        self.context = Context(self.owned_runtime, "acceptance")
        self.plugin = WebPlugin(self)

    def quiet_active(self):
        return False

    def audio(self, pcm):
        if self.capture is not None:
            self.capture.append(pcm)
        if np.max(np.abs(np.frombuffer(pcm, dtype="<i2").astype(np.int32)), initial=0) > 300:
            self.voiced_frames += 1

    async def event(self, e):
        if e["kind"] == "transcript":
            self.transcripts.append(e)
            print("voice", e["role"], e["text"], flush=True)

    async def speak(self, text):
        await self.voice.say(text)

    async def tool_control(self, *args):
        return {"ok": True}

    async def control(self, cmd):
        if cmd.kind == "cancel":
            await self.backend.cancel()
        elif cmd.kind == "stop":
            self.mute = True

    async def start(self):
        await self.backend.start()
        assert (await self.backend.account()).get("type") == "chatgpt"
        await self.conversation.start(self.context)
        await self.plugin.start(self.context)

    async def close(self):
        await self.voice.close()
        await self.context.close()
        await self.backend.close()
        self.store.db.close()


async def until(predicate, timeout=120):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.1)


async def run(config):
    evidence = config.state_dir / "acceptance/web"
    evidence.mkdir(parents=True, exist_ok=True)
    result = {"started": time.time(), "passed": False}
    with tempfile.TemporaryDirectory(prefix="web-acceptance-", dir=config.state_dir) as folder:
        root = Path(folder)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        isolated = replace(
            config,
            root=root,
            workspace=str(root),
            quiet_hours={},
            memory_auto_capture=False,
            web={"enable": True, "host": "127.0.0.1", "port": port},
        )
        host = AcceptanceHost(isolated)
        try:
            await host.start()
            result["session_id"] = host.backend.session_id
            token = (isolated.state_dir / "web-access-token").read_text().strip()
            origin = f"http://127.0.0.1:{port}"
            image = Image.new("RGB", (640, 420), "white")
            ImageDraw.Draw(image).ellipse((220, 110, 420, 310), fill="#2463d5")
            image.save(root / "blue-circle.png")
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(headless=True)
                ca = await browser.new_context(viewport={"width": 1280, "height": 900})
                cb = await browser.new_context(
                    viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True
                )
                a = await ca.new_page()
                b = await cb.new_page()
                errors = []
                for page in (a, b):
                    page.on("pageerror", lambda e: errors.append(str(e)))
                    await page.goto(origin)
                    await page.get_by_label("访问口令").fill(token)
                    await page.get_by_role("button", name="进入对话").click()
                    await page.get_by_label("消息", exact=True).wait_for()

                async def send(text):
                    await a.get_by_label("消息", exact=True).fill(text)
                    await a.get_by_role("button", name="发送", exact=True).click()
                    await until(lambda: host.backend.busy or host.conversation.web_dispatching)
                    await until(lambda: not host.backend.busy and not host.conversation.web_dispatching)
                    await asyncio.sleep(0.7)

                async def model_answer():
                    rows = host.conversation.store.history(host.backend.session_id)
                    return "\n".join(
                        m["text"] for m in rows if m["source"] == "task" and m["kind"] == "message"
                    )

                print("text and duplicate submission", flush=True)
                ident = str(uuid.uuid4())
                payload = {
                    "request_id": ident,
                    "text": "本次临时口令是枫叶四十二，请只回复知道了。",
                    "attachments": [],
                }
                await a.evaluate(
                    'async p => Promise.all([1,2].map(()=>fetch("/api/messages",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(p)}).then(r=>r.json())))',
                    payload,
                )
                await until(lambda: host.backend.busy or host.conversation.web_dispatching)
                await until(lambda: not host.backend.busy and not host.conversation.web_dispatching)
                rows = host.conversation.store.history(host.backend.session_id)
                assert sum(m["id"] == "web:" + ident for m in rows) == 1
                await b.locator('[data-message-id="web:' + ident + '"]').wait_for()
                result["duplicate_request_single_message"] = True
                print("image understanding and browser sync", flush=True)
                await a.locator("input[type=file]").set_input_files(str(root / "blue-circle.png"))
                await a.locator(".attachments img").wait_for()
                await send("请描述这张图的主要颜色和形状，简短回答。")
                answer = await model_answer()
                assert "蓝" in answer and "圆" in answer, answer
                await b.locator(".message-images img").wait_for()
                result["image_understanding"] = True
                print("voice/text context and silent replies", flush=True)
                await host.voice.open()
                host.mode = "active"
                await host.voice.send_text("刚才网页的图片是什么颜色和形状？简短回答。")
                await until(
                    lambda: any(e["role"] == "assistant" and "蓝" in e["text"] for e in host.transcripts), 45
                )
                await until(
                    lambda: any(e["role"] == "assistant" and "圆" in e["text"] for e in host.transcripts), 10
                )
                result["web_image_to_voice"] = True
                # Feed an actual synthetic utterance, not a web text surrogate.
                await asyncio.sleep(2)
                host.capture = []
                voiced = host.voiced_frames
                await host.voice.say(
                    "请逐字朗读下面这句话作为音频样本，不添加其他内容：这次验收的代号是青松十七。"
                )
                await until(lambda: host.voiced_frames > voiced + 10, 25)
                await asyncio.sleep(12)
                pcm = b"".join(host.capture)
                host.capture = None
                print("synthetic audio bytes", len(pcm), flush=True)
                await host.voice.close()
                await host.voice.open()
                samples = (
                    soxr.resample(np.frombuffer(pcm, dtype="<i2").astype(np.float32), 24000, 16000)
                    .clip(-32768, 32767)
                    .astype("<i2")
                    .tobytes()
                )
                for offset in range(0, len(samples) + 32000, 640):
                    host.voice.feed(samples[offset : offset + 640] if offset < len(samples) else bytes(640))
                    await asyncio.sleep(0.02)
                await until(
                    lambda: any(e["role"] == "user" and "十七" in e["text"] for e in host.transcripts), 35
                )
                await asyncio.sleep(3)
                before = host.voiced_frames
                voice_count = len(host.transcripts)
                await send("我刚才通过语音说的验收代号是什么？只回答代号。")
                await asyncio.sleep(3)
                answer = await model_answer()
                assert "青松十七" in answer, answer
                assert not [e for e in host.transcripts[voice_count:] if e["role"] == "assistant"], (
                    host.transcripts[voice_count:]
                )
                assert host.voiced_frames - before < 5, host.voiced_frames - before
                result["voice_to_web"] = True
                result["web_reply_silent"] = True
                # A real voice input must restore output after a silent Web reply.
                before = host.voiced_frames
                await host.voice.send_text("现在请用语音回答：一加一等于几？")
                await until(lambda: host.voiced_frames > before + 5, 30)
                result["voice_resumes_after_web"] = True
                await host.voice.close()
                host.mode = "standby"
                print("file task and offline replay", flush=True)
                await cb.set_offline(True)
                await send("请在当前目录创建 web-acceptance.txt，内容为 shared-session-ok。完成后简短回复。")
                assert (root / "web-acceptance.txt").read_text().strip() == "shared-session-ok"
                await cb.set_offline(False)
                latest = [
                    m
                    for m in host.conversation.store.history(host.backend.session_id)
                    if m["source"] == "task" and m["kind"] == "message"
                ][-1]
                await b.locator('[data-message-id="' + latest["id"] + '"]').wait_for(timeout=20000)
                result["file_task"] = True
                result["offline_replay"] = True
                print("steering and explicit cancellation", flush=True)
                await a.get_by_label("消息", exact=True).fill(
                    "请执行一个 Python 命令，先 sleep 12 秒，然后在当前目录写入 steer-base.txt，内容 base。不要后台运行；完成后回复。"
                )
                await a.get_by_role("button", name="发送", exact=True).click()
                await until(lambda: bool(host.backend.command_items.get(host.backend.active_turn)))
                turn = host.backend.active_turn
                await send("补充要求：完成上一步后，再创建 steer-extra.txt，内容 extra。继续当前任务。")
                assert (root / "steer-base.txt").exists()
                assert (root / "steer-extra.txt").read_text().strip() == "extra"
                assert host.backend.last_turn == turn
                result["supplement_steers_same_turn"] = True
                await a.get_by_label("消息", exact=True).fill(
                    "请执行 Python 命令，先 sleep 25 秒，然后创建 cancel-marker.txt。不要后台运行，等完成再回答。"
                )
                await a.get_by_role("button", name="发送", exact=True).click()
                await until(lambda: bool(host.backend.command_items.get(host.backend.active_turn)))
                await a.get_by_role("button", name="取消任务", exact=True).click()
                await until(lambda: not host.backend.busy)
                await asyncio.sleep(26)
                assert not (root / "cancel-marker.txt").exists()
                result["cancel_stops_owned_execution"] = True
                await a.screenshot(path=str(evidence / "desktop.png"), full_page=True)
                await b.screenshot(path=str(evidence / "mobile.png"), full_page=True)
                assert await b.evaluate("document.documentElement.scrollWidth <= innerWidth"), (
                    "Mobile horizontal overflow"
                )
                print("service restart persistence", flush=True)
                session = host.backend.session_id
                await host.close()
                host = AcceptanceHost(isolated)
                await host.start()
                assert host.backend.session_id == session
                await a.reload()
                await a.get_by_label("消息", exact=True).wait_for()
                await a.locator('[data-message-id="' + latest["id"] + '"]').wait_for()
                result["restart_preserves_session_and_login"] = True
                assert not errors, errors
                result["browser_errors"] = errors
                result["passed"] = True
                await browser.close()
        finally:
            if not result["passed"]:
                result["last_messages"] = host.conversation.store.history(host.backend.session_id)[-8:]
            await host.close()
            result["finished"] = time.time()
            (evidence / "acceptance.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
            print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.toml")
    args = parser.parse_args()
    asyncio.run(run(Config.load(args.config)))


if __name__ == "__main__":
    main()
