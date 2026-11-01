"""Browser check for tool status and collapsed explanations; synthetic local data only."""

import argparse
import asyncio
from pathlib import Path
from aiohttp import web
from playwright.async_api import async_playwright, expect


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--browser")
    args = parser.parse_args()
    static = Path(__file__).resolve().parents[1] / "src/assistant_runtime/plugins/web/static"
    app = web.Application()
    app.router.add_get("/chat/", lambda _: web.FileResponse(static / "index.html"))
    app.router.add_static("/chat/", static)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    state = dict(
        name="Test",
        voice="disabled",
        speaking=False,
        task_running=False,
        quiet=False,
        session_id="test",
        web_dispatching=False,
        questions=[],
        queue=[],
    )
    cases = [
        ("invalid_arguments", "not_dispatched", "failed", "validation", "未执行"),
        ("tool_permission_denied", "not_dispatched", "failed", "authorization", "被禁止"),
        ("tool_result_unconfirmed", "dispatched", "unknown", "return", "结果待确认"),
        ("business_failed", "dispatched", "failed", "return", "调用失败"),
    ]
    messages = [
        dict(
            id=str(i),
            ordinal=i,
            role="assistant",
            source="assistant",
            kind="detail",
            text="调用工具\nsample_" + str(i),
            status="failed",
            attachments=[],
            updated=i,
            turn_id="one",
            tool_summary=dict(
                kind="tool",
                outcome="failed",
                error_code=code,
                dispatch=dispatch,
                result_state=result,
                stage=stage,
            ),
        )
        for i, (code, dispatch, result, stage, _) in enumerate(cases, 1)
    ]
    errors = []
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(**({"executable_path": args.browser} if args.browser else {}))
            page = await browser.new_page(viewport={"width": 1366, "height": 900})
            page.on("pageerror", lambda error: errors.append(str(error)))
            await page.add_init_script("window.EventSource = class extends EventTarget { close() {} };")

            async def route_api(route):
                path = route.request.url.split("/api/")[1].split("?")[0]
                await route.fulfill(
                    json={"messages": messages, "state": state, "cursor": 0, "before": None}
                    if path == "messages"
                    else state
                    if path == "session"
                    else {}
                )

            await page.route("**/api/**", route_api)
            await page.goto(f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/chat/")
            for *_, label in cases:
                await expect(page.get_by_text(label, exact=True)).to_have_count(1)
            explanations = page.locator("details.tool-result")
            await expect(explanations).to_have_count(4)
            assert await explanations.evaluate_all("xs => xs.every(x => !x.open)")
            await explanations.first.locator("summary").click()
            assert await explanations.first.evaluate("x => x.open")
            await expect(page.get_by_text("参数不符合工具要求，操作未执行。", exact=False)).to_be_visible()
            assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            assert not errors, errors
            await browser.close()
    finally:
        await runner.cleanup()
    print("Tool outcomes: four distinct states, collapsed explanations, no browser errors or overflow")


if __name__ == "__main__":
    asyncio.run(main())
