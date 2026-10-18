"""Repeatable isolated subscription/browser acceptance; never uses room devices or user history."""

import argparse
import asyncio
import io
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
from assistant_runtime.cli import request
from assistant_runtime.plugins.web.settings import WebSettings


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


async def check_reply_images(config):
    """Real subscription/MCP/browser path with a synthetic large camera image."""
    from assistant_runtime.kernel.contracts import VideoFrame

    evidence = config.state_dir / "acceptance/web-images"
    evidence.mkdir(parents=True, exist_ok=True)
    result = {"passed": False, "synthetic_camera": True, "started": time.time()}
    with tempfile.TemporaryDirectory(prefix="reply-images-") as folder:
        root = Path(folder)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        spec = {"adapter": "acceptance", "kind": "camera", "label": "验收相机", "connection": {}}
        isolated = replace(
            config,
            root=root,
            workspace=str(root),
            codex_sandbox="read-only",
            devices={"fixture_camera": spec},
            plugins=[*config.plugins, "acceptance"],
            web={"enable": True, "host": "127.0.0.1", "port": port},
        )
        host = AcceptanceHost(isolated)
        source = io.BytesIO()
        Image.effect_noise((640, 480), 100).convert("RGB").save(source, format="JPEG", quality=92)
        frame = source.getvalue()
        captures = 0

        async def connection(reader, writer):
            nonlocal captures
            try:
                d = json.loads(await reader.readline())
                if d.get("command") != "devices":
                    raise ValueError("Camera fixture permits only device queries/capture")
                if d["action"] == "snapshot" and d["device_id"] == "fixture_camera":
                    captures += 1
                    data = await host.conversation.capture_reply(
                        VideoFrame("fixture_camera", frame, 640, 480, time.time(), captures)
                    )
                elif d["action"] == "list":
                    data = {
                        "devices": [
                            {
                                "id": "fixture_camera",
                                "label": "验收相机",
                                "kind": "camera",
                                "capabilities": ["video.snapshot"],
                                "state": "available",
                            }
                        ]
                    }
                else:
                    raise ValueError("Unexpected fixture action")
                reply = {"ok": True, "result": data}
            except Exception as exc:
                reply = {"ok": False, "error": str(exc)}
            writer.write((json.dumps(reply) + "\n").encode())
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        server = await asyncio.start_unix_server(connection, path=isolated.socket)
        try:
            await host.start()
            token = (isolated.state_dir / "web-access-token").read_text().strip()
            origin = f"http://127.0.0.1:{port}"
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(headless=True)
                contexts = [
                    await browser.new_context(viewport={"width": 1280, "height": 900}),
                    await browser.new_context(
                        viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True
                    ),
                ]
                pages = [await context.new_page() for context in contexts]
                errors = []
                for page in pages:
                    page.on("pageerror", lambda e: errors.append(str(e)))
                    await page.goto(origin)
                    await page.locator('input[type="password"]').fill(token)
                    await page.get_by_role("button", name="进入对话", exact=True).click()
                    await page.get_by_label("消息", exact=True).wait_for()
                a, b = pages
                await a.get_by_label("消息", exact=True).fill(
                    "验收图片显示。必须使用 device MCP 的 list_devices 和 capture_image(device_id=fixture_camera) 返回一张截图，然后简短回复已返回。只用这两个工具，不用shell、记忆或其它控制，不需要分析图片。"
                )
                await a.get_by_role("button", name="发送", exact=True).click()
                await until(lambda: host.backend.busy or host.conversation.web_dispatching)
                await until(lambda: not host.backend.busy and not host.conversation.web_dispatching)
                assert captures == 1
                for page in pages:
                    await page.locator("article.assistant .message-images img").first.wait_for()
                    await page.locator(
                        "article.assistant .message-images img"
                    ).first.scroll_into_view_if_needed()
                    await page.wait_for_function(
                        "() => document.querySelector('article.assistant .message-images img').naturalWidth === 640"
                    )
                asset = a.locator("article.assistant .message-images img").first
                src = await asset.get_attribute("src")
                assert src == await b.locator("article.assistant .message-images img").first.get_attribute(
                    "src"
                )
                assert await asset.evaluate("i => !i.closest('details')")
                response = await a.request.get(origin + src)
                assert len(await response.body()) > 65000
                await asset.click()
                await a.locator(".lightbox img").wait_for()
                await a.wait_for_function(
                    "() => document.querySelector('.lightbox img').naturalWidth === 640"
                )
                await a.keyboard.press("Escape")
                await b.reload()
                await b.locator("article.assistant .message-images img").first.wait_for()
                assert src == await b.locator("article.assistant .message-images img").first.get_attribute(
                    "src"
                )
                session = host.backend.session_id
                # Exercise recovery of a missing projection using the real stored
                # provider result; do not call the camera fixture a second time.
                with host.store.db:
                    host.store.db.execute("UPDATE chat_messages SET attachments='[]' WHERE role='assistant'")
                await host.close()
                host = AcceptanceHost(isolated)
                await host.start()
                assert host.backend.session_id == session
                await host.conversation.image_queue.join()
                await a.reload()
                await a.locator("article.assistant .message-images img").first.wait_for()
                await a.locator("article.assistant .message-images img").first.scroll_into_view_if_needed()
                await a.wait_for_function(
                    "() => document.querySelector('article.assistant .message-images img').naturalWidth === 640"
                )
                assert src == await a.locator("article.assistant .message-images img").first.get_attribute(
                    "src"
                )
                assert captures == 1
                assert not errors, errors
                assert host.store.db.execute("SELECT count(*) FROM chat_uploads").fetchone()[0] == 1
                await browser.close()
                result.update(
                    passed=True,
                    captures=captures,
                    original_bytes=len(frame),
                    full_image_served=True,
                    preview=True,
                    desktop_mobile_sync=True,
                    reload=True,
                    restart_backfill_without_recapture=True,
                    browser_errors=errors,
                )
        finally:
            await host.close()
            server.close()
            await server.wait_closed()
            result["finished"] = time.time()
            (evidence / "subscription.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
            print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)


async def check_installed_images(config):
    """Read-only browser checks against the installed loopback UI; no room capture."""
    settings = WebSettings.parse(config.web)
    if settings.host not in {"0.0.0.0", "127.0.0.1"}:
        raise ValueError("Installed browser acceptance requires a local loopback listener")
    origin = f"http://127.0.0.1:{settings.port}"
    token = settings.credential_path(config.state_dir).read_text().strip()
    before = await request(config, {"command": "status"})
    captures = {d["id"]: d["data"].get("capture_count") for d in before["devices"] if d["kind"] == "camera"}
    result = {"passed": False, "started": time.time(), "fresh_capture_requested": False}
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            desktop = await browser.new_context(viewport={"width": 1280, "height": 900})
            mobile = await browser.new_context(
                viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True
            )
            a, b = await desktop.new_page(), await mobile.new_page()
            errors = []
            for page in (a, b):
                page.on("pageerror", lambda e: errors.append(str(e)))
                await page.goto(origin)
                await page.locator('input[type="password"]').fill(token)
                await page.get_by_role("button", name="进入对话", exact=True).click()
                await page.locator("article.assistant .message-images img").first.wait_for()
                await page.wait_for_function("""() => {
                    const images = [...document.querySelectorAll('article.assistant .message-images img')];
                    return images.length && images.every(i => i.complete && i.naturalWidth > 0);
                }""")

            def image_state(page):
                return page.locator("article.assistant .message-images img").evaluate_all(
                    "images => images.map(i => ({src: i.getAttribute('src'), width: i.naturalWidth, height: i.naturalHeight, outsideDetails: !i.closest('details')}))"
                )

            first = await image_state(a)
            assert first == await image_state(b)
            assert all(i["outsideDetails"] for i in first)
            await a.locator("article.assistant .message-images img").first.click()
            await a.locator(".lightbox img").wait_for()
            await a.wait_for_function("() => document.querySelector('.lightbox img').naturalWidth > 0")
            await a.keyboard.press("Escape")
            await a.locator(".lightbox").wait_for(state="detached")
            await b.reload()
            await b.locator("article.assistant .message-images img").first.wait_for()
            await b.wait_for_function(
                "() => [...document.querySelectorAll('article.assistant .message-images img')].every(i => i.complete && i.naturalWidth > 0)"
            )
            assert first == await image_state(b)
            await desktop.set_offline(True)
            await desktop.set_offline(False)
            await a.reload()
            await a.locator("article.assistant .message-images img").first.wait_for()
            await a.wait_for_function(
                "() => [...document.querySelectorAll('article.assistant .message-images img')].every(i => i.complete && i.naturalWidth > 0)"
            )
            assert first == await image_state(a)
            assert not errors, errors
            # Sign out these test browser sessions only; never output credentials/cookies.
            for page in (a, b):
                response = await page.request.post(
                    origin + "/api/logout", headers={"Origin": origin}, data={}
                )
                assert response.status == 200
            await browser.close()
            result.update(
                assistant_images=first,
                desktop_mobile_sync=True,
                image_preview=True,
                reload_and_network_recovery=True,
                browser_errors=errors,
            )
        after = await request(config, {"command": "status"})
        assert after["session_id"] == before["session_id"]
        assert after["capture_recent"]
        assert captures == {
            d["id"]: d["data"].get("capture_count") for d in after["devices"] if d["kind"] == "camera"
        }
        result["passed"] = True
    finally:
        result["finished"] = time.time()
        path = config.state_dir / "acceptance/web-images/installed.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(result, ensure_ascii=False, indent=2))
        path.chmod(0o600)
        print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config/config.toml")
    parser.add_argument(
        "--installed-images",
        action="store_true",
        help="Verify existing assistant images in the installed loopback UI; no new capture or model task",
    )
    parser.add_argument(
        "--reply-images",
        action="store_true",
        help="Real Codex/MCP/browser image regression with a synthetic camera; no room capture",
    )
    args = parser.parse_args()
    if args.reply_images and args.installed_images:
        parser.error("Choose one image acceptance mode")
    verify = (
        check_reply_images
        if args.reply_images
        else (check_installed_images if args.installed_images else run)
    )
    asyncio.run(verify(Config.load(args.config)))


if __name__ == "__main__":
    main()
