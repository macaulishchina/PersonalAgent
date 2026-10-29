"""Browser acceptance for composer routing/layout using local synthetic responses."""

import asyncio
import base64
import json
from pathlib import Path
import tempfile

from aiohttp import web
from playwright.async_api import async_playwright, expect


async def main():
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
    output = Path(tempfile.mkdtemp(prefix="pa-composer-ui-"))
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(channel="msedge", headless=True)
            page = await browser.new_page(viewport={"width": 1366, "height": 900}, color_scheme="dark")
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
            await expect(page.get_by_role("button", name="加入队列", exact=True)).to_be_disabled()
            await entry.fill("请把这项要求也考虑进去")
            await page.locator(".composer-wrap").screenshot(path=str(output / "running-dark.png"))
            await entry.press("Enter")
            await expect(entry).to_have_value("")
            assert posts[-1][0] == "messages"
            await entry.fill("优先处理第一部分")
            await page.get_by_role("button", name="补充执行", exact=True).click()
            await expect(page.get_by_role("button", name="发送补充", exact=True)).to_be_enabled()
            await expect(page.get_by_role("button", name="添加图片", exact=True)).to_be_disabled()
            await entry.press("Enter")
            await expect(entry).to_have_value("")
            assert posts[-1] == (
                "conversation",
                {"action": "supplement", "request_id": "active-one", "text": "优先处理第一部分"},
            )

            async def update():
                await page.evaluate(
                    "s => window.fixtureEvents.dispatchEvent(new MessageEvent('update', {data: JSON.stringify({seq: 1, kind: 'state', data: s})}))",
                    state,
                )

            state.update(
                question_id="question-one",
                questions=[{"id": "question-one", "question": "先完成方案设计，还是直接开始实现？"}],
            )
            await update()
            await page.get_by_role("button", name="回答问题", exact=True).click()
            await entry.fill("先完成方案设计")
            await page.locator(".composer-wrap").screenshot(path=str(output / "question-dark.png"))
            await page.get_by_role("button", name="发送回答", exact=True).click()
            await expect(entry).to_have_value("")
            assert posts[-1][1]["question_id"] == "question-one"
            assert posts[-1][1]["action"] == "answer"

            await page.get_by_role("button", name="回答问题", exact=True).click()
            state.update(active_request_id="active-next", question_id=None, questions=[])
            await update()
            await expect(page.get_by_role("button", name="新消息", exact=True)).to_have_attribute(
                "aria-pressed", "true"
            )
            await entry.fill("保留这份草稿")
            await page.locator(".composer-queue summary").click()
            await page.get_by_role("button", name="取消第 1 条排队消息", exact=True).click()
            await expect(entry).to_have_value("保留这份草稿")
            assert posts[-1][1] == {"action": "cancel", "request_id": "queued-two"}
            await page.get_by_role("button", name="取消当前执行", exact=True).click()
            await expect(page.get_by_role("button", name="取消当前执行", exact=True)).to_be_enabled()
            assert posts[-1][1] == {"action": "cancel", "request_id": "active-next"}
            await expect(entry).to_have_value("保留这份草稿")
            await entry.press("Shift+Enter")
            await expect(entry).to_have_value("保留这份草稿\n")

            await page.locator('input[type="file"]').set_input_files(
                {
                    "name": "test.png",
                    "mimeType": "image/png",
                    "buffer": base64.b64decode(
                        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aPFsAAAAASUVORK5CYII="
                    ),
                }
            )
            await expect(page.get_by_role("button", name="补充执行", exact=True)).to_be_disabled()
            await page.get_by_role("button", name="移除 test.png", exact=True).click()
            await expect(page.get_by_role("button", name="补充执行", exact=True)).to_be_enabled()
            await page.emulate_media(color_scheme="light")
            await page.locator(".composer-wrap").screenshot(path=str(output / "running-light.png"))
            for width in (1024, 1366, 1920):
                await page.set_viewport_size({"width": width, "height": 900})
                assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            await page.evaluate("window.fixtureEvents.onerror()")
            await expect(page.get_by_role("button", name="加入队列", exact=True)).to_be_disabled()
            await expect(page.get_by_role("button", name="取消当前执行", exact=True)).to_be_disabled()
            assert not errors, errors
            await browser.close()
    finally:
        await runner.cleanup()
    print(
        json.dumps(
            {
                "composer_routes_keyboard_attachments_disconnect": "passed",
                "desktop_layout": "passed",
                "screenshots": str(output),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
