"""Explicit shared Web/Codex recording acceptance with actual browser playback."""

import argparse
import asyncio
import json
import time
import uuid

from playwright.async_api import async_playwright

from assistant_runtime.cli import request
from assistant_runtime.config import Config
from assistant_runtime.plugins.web.settings import WebSettings


async def verify(config, devices, duration):
    settings = WebSettings.parse(config.web)
    if not settings.enable:
        raise ValueError("Web must be enabled for this playback acceptance")
    before = await request(config, {"command": "status"})
    if before["task_running"]:
        raise RuntimeError("Wait for the current foreground task before this acceptance")
    for device in devices:
        result = await request(config, {"command": "devices", "action": "inspect", "device_id": device})
        assert "video.record" in result["device"]["capabilities"]
    folder = config.state_dir / "acceptance/video-clips"
    folder.mkdir(parents=True, exist_ok=True)
    base = f"http://127.0.0.1:{settings.port}"
    evidence = {"started_at": time.time(), "devices": devices, "duration_seconds": duration, "videos": []}
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(
                headless=True, args=["--autoplay-policy=no-user-gesture-required"]
            )
            try:
                context = await browser.new_context(viewport={"width": 1100, "height": 900})
                page = await context.new_page()
                await page.goto(base)
                response = await page.request.post(
                    base + "/api/login",
                    data={"token": settings.credential_path(config.state_dir).read_text().strip()},
                    headers={"Origin": base},
                )
                assert response.status == 200
                await page.reload()
                await page.locator(".connection:not(.disconnected)").wait_for(timeout=15000)
                evidence["request_id"] = str(uuid.uuid4())
                receipt = await page.request.post(
                    base + "/api/messages",
                    data={
                        "request_id": evidence["request_id"],
                        "text": f"本次短视频功能验收：请先查询设备，用 device MCP 的 record_video 给这些摄像头各录{duration}秒："
                        + ", ".join(devices)
                        + "。每台只录一次，将视频作为聊天附件返回。不要截图、看图、移动镜头、修改音箱或音量，不要播音。视频不发送到外部服务。完成后简短告知。",
                        "attachments": [],
                    },
                    headers={"Origin": base},
                )
                assert receipt.status == 202
                user = (await receipt.json())["message"]
                started = time.monotonic()
                async with asyncio.timeout(240):
                    while True:
                        response = await page.request.get(base + "/api/messages")
                        messages = (await response.json())["messages"]
                        videos = [
                            (m, a)
                            for m in messages
                            if m["ordinal"] > user["ordinal"]
                            for a in m["attachments"]
                            if a.get("type") == "video/mp4"
                        ]
                        if len(videos) >= len(devices):
                            break
                        await asyncio.sleep(0.5)
                assert len(videos) == len(devices), "Unexpected repeated recording"
                for message, attachment in videos:
                    player = page.locator(f'[data-message-id="{message["id"]}"] video')
                    await player.wait_for(timeout=10000)
                    await player.evaluate("""v => new Promise((resolve,reject) => {
                      if (v.readyState >= 1) return resolve(true);
                      v.addEventListener('loadedmetadata', () => resolve(true), {once:true});
                      v.addEventListener('error', () => reject(Error('Video metadata failed')), {once:true});
                      setTimeout(() => reject(Error('Video metadata timeout')), 15000);
                    })""")
                    playback = await player.evaluate("""async v => {
                      await v.play();
                      await new Promise(r => setTimeout(r, 500));
                      const playing = v.currentTime > 0 && !v.error;
                      v.pause();
                      const seek = new Promise((resolve,reject) => {
                        v.addEventListener('seeked', () => resolve(true), {once:true});
                        setTimeout(() => reject(Error('Video seek timeout')), 8000);
                      });
                      v.currentTime = v.duration / 2;
                      await seek;
                      return {playing, duration:v.duration, width:v.videoWidth, height:v.videoHeight, sought:v.currentTime};
                    }""")
                    assert playback["playing"] and playback["width"] > 0 and playback["sought"] > 0
                    response = await page.request.get(
                        base + "/api/uploads/" + attachment["id"], headers={"Range": "bytes=0-127"}
                    )
                    assert response.status == 206 and len(await response.body()) == 128
                    evidence["videos"].append(
                        {
                            "message_id": message["id"],
                            "attachment": attachment,
                            "playback": playback,
                            "range_status": response.status,
                        }
                    )
                    print("Live browser playback:", attachment["name"], playback, flush=True)
                evidence["visible_without_refresh"] = True
                evidence["first_results_seconds"] = round(time.monotonic() - started, 2)
                await page.reload()
                await page.locator(".connection:not(.disconnected)").wait_for(timeout=15000)
                for item in evidence["videos"]:
                    await page.locator(f'[data-message-id="{item["message_id"]}"] video').wait_for(
                        timeout=15000
                    )
                evidence["history_reload_preserved"] = True
                guest = await browser.new_context()
                response = await guest.request.get(
                    base + "/api/uploads/" + evidence["videos"][0]["attachment"]["id"]
                )
                assert response.status == 401
                await guest.close()
                evidence["unauthorized_playback_rejected"] = True
                await page.locator("video").last.screenshot(path=str(folder / "player.png"))
            finally:
                await browser.close()
        after = await request(config, {"command": "status"})
        assert (
            after["capture_recent"]
            and after["session_id"] == before["session_id"]
            and after["voice_io"] == before["voice_io"]
        )
        evidence.update(passed=True, microphone_preserved=True, session_preserved=True)
    except BaseException as exc:
        evidence.update(passed=False, error=f"{type(exc).__name__}: {exc}")
        raise
    finally:
        evidence["finished_at"] = time.time()
        p = folder / "installed-web.json"
        p.write_text(json.dumps(evidence, ensure_ascii=False, indent=2))
        p.chmod(0o600)
        print(
            json.dumps(
                {
                    k: evidence.get(k)
                    for k in (
                        "passed",
                        "visible_without_refresh",
                        "history_reload_preserved",
                        "unauthorized_playback_rejected",
                        "microphone_preserved",
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
    parser.add_argument("--device", action="append", required=True)
    parser.add_argument("--duration", type=int, choices=range(1, 61), default=3, metavar="1..60")
    args = parser.parse_args()
    asyncio.run(verify(Config.load(args.config), args.device, args.duration))
