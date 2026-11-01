"""Browser acceptance for composer routing/layout using local synthetic responses."""

import argparse
import asyncio
import base64
import json
from pathlib import Path
import tempfile

from aiohttp import web
from playwright.async_api import async_playwright, expect


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--browser", help="Use an existing Chromium executable")
    args = parser.parse_args()
    static = Path(__file__).resolve().parents[1] / "src/assistant_runtime/plugins/web/static"
    app = web.Application()
    app.router.add_get("/chat/", lambda request: web.FileResponse(static / "index.html"))
    app.router.add_static("/chat/", static)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    origin = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
    state = dict(
        name="Personal Agent",
        voice="disabled",
        speaking=False,
        task_running=True,
        quiet=False,
        session_id="test",
        web_dispatching=False,
        active_request_id="active-one",
        questions=[],
        queue=[{"id": "queued-two", "state": "queued"}],
    )
    posts, errors = [], []
    failure = {"next": False}
    output = Path(tempfile.mkdtemp(prefix="pa-composer-ui-"))
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(
                **({"executable_path": args.browser} if args.browser else {}), headless=True
            )
            page = await browser.new_page(
                viewport={"width": 1366, "height": 900}, color_scheme="dark", has_touch=True
            )
            page.on("pageerror", lambda error: errors.append(str(error)))
            await page.add_init_script("""window.EventSource = class extends EventTarget {
                constructor() { super(); window.fixtureEvents = this; setTimeout(() => this.onopen?.(), 0); }
                close() {}
            };""")

            async def route_api(route):
                path = route.request.url.split("/api/")[1].split("?")[0]
                data = {}
                if route.request.method == "POST":
                    if path == "uploads":
                        data = {"id": "image-one", "name": "test.png"}
                    else:
                        body = route.request.post_data_json
                        posts.append((path, body))
                        if path == "conversation" and failure["next"]:
                            failure["next"] = False
                            await route.fulfill(
                                status=409, json={"error": "当前执行已结束，请确认后重新发送。"}
                            )
                            return
                        if path == "messages":
                            data = {
                                "message": dict(
                                    id="sent-one",
                                    ordinal=1,
                                    role="user",
                                    source="web",
                                    kind="message",
                                    text=body["text"],
                                    status="queued",
                                    attachments=[],
                                    updated=1,
                                )
                            }
                elif path == "messages":
                    data = {"messages": [], "state": state, "cursor": 0, "before": None}
                elif path == "session":
                    data = state
                elif path.startswith("uploads/"):
                    await route.fulfill(
                        content_type="image/png",
                        body=base64.b64decode(
                            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aPFsAAAAASUVORK5CYII="
                        ),
                    )
                    return
                await route.fulfill(json=data)

            await page.route("**/api/**", route_api)
            await page.goto(origin + "/chat/")
            entry = page.get_by_role("textbox", name="消息", exact=True)

            switch = page.get_by_role("switch", name="直接引导", exact=True)

            async def choose(mode):
                expected = "true" if mode == "直接引导" else "false"
                if await switch.get_attribute("aria-checked") != expected:
                    await switch.click()
                await expect(switch).to_have_attribute("aria-checked", expected)
                await expect(page.get_by_role("menu")).to_have_count(0)

            async def update():
                await page.evaluate(
                    "s => window.fixtureEvents.dispatchEvent(new MessageEvent('update', {data: JSON.stringify({seq: 1, kind: 'state', data: s})}))",
                    state,
                )

            # A fresh browser directs the active execution; sending never changes the preference.
            await expect(page.get_by_role("button", name="直接引导", exact=True)).to_be_disabled()
            await expect(page.locator(".composer-context")).to_have_count(0)
            await expect(page.locator(".delivery-modes")).to_have_count(0)
            await entry.fill("请把这项要求也考虑进去")
            await page.locator(".composer-wrap").screenshot(path=str(output / "running-dark.png"))
            await entry.press("Enter")
            await expect(entry).to_have_value("")
            assert posts[-1] == (
                "conversation",
                {"action": "supplement", "request_id": "active-one", "text": "请把这项要求也考虑进去"},
            )
            await expect(page.get_by_role("button", name="添加图片", exact=True)).to_be_disabled()
            await choose("排队")
            await entry.fill("下一轮处理这件事")
            await page.reload()
            await expect(entry).to_have_value("下一轮处理这件事")
            await expect(switch).to_have_attribute("aria-checked", "false")
            await entry.press("Enter")
            await expect(entry).to_have_value("")
            assert posts[-1][0] == "messages"

            # Space and Enter switch in place, without opening a menu.
            await switch.press("Space")
            await expect(switch).to_have_attribute("aria-checked", "true")
            await page.reload()
            await expect(switch).to_have_attribute("aria-checked", "true")
            await switch.press("Enter")
            await expect(switch).to_have_attribute("aria-checked", "false")
            await switch.press("Enter")
            await expect(switch).to_have_attribute("aria-checked", "true")
            await expect(page.get_by_role("menu")).to_have_count(0)

            state.update(
                question_id="question-one",
                questions=[{"id": "question-one", "question": "先完成方案设计，还是直接开始实现？"}],
            )
            await update()
            await entry.fill("先完成方案设计")
            await page.locator(".composer-wrap").screenshot(path=str(output / "question-dark.png"))
            await page.get_by_role("button", name="发送回答", exact=True).click()
            await expect(entry).to_have_value("")
            assert posts[-1][1]["question_id"] == "question-one"
            assert posts[-1][1]["action"] == "answer"
            await expect(switch).to_be_enabled()

            # An execution change targets the new execution without resetting the choice.
            state.update(active_request_id="active-next", question_id=None, questions=[])
            await update()
            await entry.fill("调整新执行的方向")
            await entry.press("Enter")
            await expect(entry).to_have_value("")
            assert posts[-1][1]["request_id"] == "active-next"
            assert posts[-1][1]["action"] == "supplement"
            failure["next"] = True
            await entry.fill("不要丢失这份补充")
            count = len(posts)
            await entry.press("Enter")
            await expect(page.get_by_role("alert")).to_contain_text("当前执行已结束")
            await expect(entry).to_have_value("不要丢失这份补充")
            assert len(posts) == count + 1 and posts[-1][0] == "conversation"
            await expect(switch).to_have_attribute("aria-checked", "true")

            await entry.fill("保留这份草稿")
            await page.locator(".composer-queue summary").click()
            await page.get_by_role("button", name="取消第 1 条排队消息", exact=True).click()
            await expect(entry).to_have_value("保留这份草稿")
            assert posts[-1][1] == {"action": "cancel", "request_id": "queued-two"}
            await page.get_by_role("button", name="停止当前执行", exact=True).click()
            await expect(page.get_by_role("button", name="停止当前执行", exact=True)).to_be_enabled()
            assert posts[-1][1] == {"action": "cancel", "request_id": "active-next"}
            await expect(entry).to_have_value("保留这份草稿")
            await entry.press("Shift+Enter")
            await expect(entry).to_have_value("保留这份草稿\n")

            # An attached image never silently changes a steering message into queued work.
            await choose("排队")
            await page.locator('input[type="file"]').set_input_files(
                {
                    "name": "test.png",
                    "mimeType": "image/png",
                    "buffer": base64.b64decode(
                        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aPFsAAAAASUVORK5CYII="
                    ),
                }
            )
            await expect(page.get_by_role("button", name="移除 test.png", exact=True)).to_be_visible()
            await choose("直接引导")
            await expect(page.get_by_role("button", name="直接引导", exact=True)).to_be_disabled()
            await expect(page.locator(".composer-delivery-note")).to_be_visible()
            count = len(posts)
            await entry.press("Enter")
            assert len(posts) == count
            await expect(entry).to_have_value("保留这份草稿\n")
            await choose("排队")
            await entry.press("Enter")
            await expect(entry).to_have_value("")
            assert posts[-1][0] == "messages" and posts[-1][1]["attachments"] == ["image-one"]
            await choose("直接引导")
            await entry.fill("继续当前执行")
            await page.emulate_media(color_scheme="light")
            await page.locator(".composer-wrap").screenshot(path=str(output / "running-light.png"))
            running_height = await page.locator(".composer").evaluate("e => e.offsetHeight")
            state.update(active_request_id=None, task_running=False, queue=[])
            await update()
            await expect(page.get_by_role("button", name="发送", exact=True)).to_be_enabled()
            idle_height = await page.locator(".composer").evaluate("e => e.offsetHeight")
            assert running_height == idle_height, (running_height, idle_height)
            await page.locator(".composer-wrap").screenshot(path=str(output / "idle-light.png"))
            await entry.press("Enter")
            await expect(entry).to_have_value("")
            assert posts[-1][0] == "messages"
            state.update(active_request_id="active-next", task_running=True)
            await update()
            for width in (320, 375, 600, 1024, 1366, 1920):
                await page.set_viewport_size({"width": width, "height": 900})
                await switch.tap()
                await expect(switch).to_have_attribute("aria-checked", "false")
                await switch.tap()
                await expect(switch).to_have_attribute("aria-checked", "true")
                await expect(page.get_by_role("menu")).to_have_count(0)
                assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth"), width
                if width == 375:
                    await page.screenshot(path=str(output / "mobile-light-switch.png"))
            await page.evaluate("window.fixtureEvents.onerror()")
            await expect(page.get_by_role("button", name="直接引导", exact=True)).to_be_disabled()
            await expect(page.get_by_role("button", name="停止当前执行", exact=True)).to_be_disabled()
            assert not errors, errors
            await browser.close()
    finally:
        await runner.cleanup()
    print(
        json.dumps(
            {
                "composer_default_steer_persistence_routing_keyboard_attachments_disconnect": "passed",
                "desktop_mobile_layout": "passed",
                "screenshots": str(output),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
