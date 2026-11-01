"""Isolated desktop model-selector acceptance; no live configuration or provider calls.

Requires Playwright and Chromium. Run from the repository with PYTHONPATH=src, or
against an installed package. The business process is never started.
"""

import argparse
import asyncio
from pathlib import Path
import socket
import tempfile
from unittest.mock import AsyncMock
from playwright.async_api import async_playwright, expect
from assistant_runtime.app.management import BusinessProcess
from assistant_runtime.core.configuration.console import Console
from assistant_runtime.core.configuration.settings import ManagementSettings


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--browser", help="Optional Chromium executable")
    parser.add_argument("--artifacts", type=Path, help="Optional private screenshot directory")
    args = parser.parse_args()
    if args.artifacts:
        args.artifacts = args.artifacts.expanduser().absolute()
        args.artifacts.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory(prefix="pa-model-picker-") as folder:
        root = Path(folder)
        entry = root / "config.toml"
        entry.write_text("""root="."
plugins=[]
main_model="model-a"
[model_api.providers.official]
preset="openai"
enabled=false
[model_api.providers.third-party]
preset="openai"
enabled=false
[model_api.entries.model-a]
provider="official"
model="same-upstream-model"
reasoning_efforts=["low","high"]
[model_api.entries.model-b]
provider="third-party"
model="same-upstream-model"
reasoning_efforts=["low","high"]
[tasks.model_policy.recommendations.fast]
name="Fast"
model="model-a"
reasoning_effort="high"
reason="synthetic"
description="synthetic"
""")
        before = entry.read_bytes()
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        business = BusinessProcess(ManagementSettings(root, entry, host="127.0.0.1", port=port))
        console = Console(business)
        console.provider_queries.references.download = AsyncMock(side_effect=OSError("offline"))
        await console.start()
        try:
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(executable_path=args.browser, headless=True)
                page = await browser.new_page(viewport={"width": 1440, "height": 1000})
                errors = []
                page.on("pageerror", lambda e: errors.append(str(e)))
                await page.goto(f"http://127.0.0.1:{port}")
                await page.get_by_label("管理员密码", exact=True).fill(
                    (business.state / "admin-token").read_text().strip()
                )
                await page.get_by_role("button", name="登录", exact=True).click()
                await expect(page.locator("#editor")).to_be_visible()
                await page.get_by_role("button", name="模型与账号", exact=True).click()
                drawer = page.locator("#entry-dialog")
                for kind, name in (
                    ("entries", "model-a"),
                    ("providers", "official"),
                    ("recommendation", "fast"),
                ):
                    await page.evaluate(
                        "([kind, name]) => kind === 'recommendation' ? openTaskRecommendation(name) : openEntity(kind, name)",
                        [kind, name],
                    )
                    for width, height in ((1024, 768), (1440, 900), (1920, 1080)):
                        await page.set_viewport_size({"width": width, "height": height})
                        await page.evaluate("showNotice('布局验收：字段校验提示')")
                        result = await drawer.evaluate("""drawer => {
                            const body = drawer.querySelector('#entry-body');
                            const heading = drawer.querySelector('.dialog-heading').getBoundingClientRect();
                            const note = drawer.querySelector('.dialog-notice').getBoundingClientRect();
                            const footer = drawer.querySelector('.drawer-footer').getBoundingClientRect();
                            const content = body.getBoundingClientRect();
                            body.scrollTop = body.scrollHeight;
                            return {
                                independentScroll: drawer.scrollTop === 0 && body.scrollWidth <= body.clientWidth,
                                noOverlap: content.y >= note.bottom && note.y >= heading.bottom && content.bottom <= footer.y + 1,
                                footerVisible: footer.bottom <= innerHeight && footer.y >= 0,
                            };
                        }""")
                        assert all(result.values()), (kind, width, height, result)
                        await drawer.locator("#entry-body").evaluate("body => body.scrollTop = 0")
                        if args.artifacts and width == 1440:
                            await drawer.screenshot(path=str(args.artifacts / f"drawer-{kind}.png"))
                    await drawer.get_by_role("button", name="取消编辑", exact=True).click()
                await page.set_viewport_size({"width": 1440, "height": 1000})
                await page.get_by_role("tab", name="用途", exact=True).click()
                model = page.get_by_label("主对话模型", exact=True)
                await expect(model).to_have_value("model-a")
                assert await model.locator("option:checked").inner_text() == "model-a · official"
                await model.click()
                popup = page.locator(".model-reference-popup:popover-open")
                await expect(popup).to_be_visible()
                await model.click()
                await expect(popup).not_to_be_visible()
                await model.click()
                await expect(popup).to_be_visible()
                await expect(popup.get_by_role("treeitem", name="official", exact=True)).to_be_visible()
                await expect(popup.get_by_role("treeitem", name="third-party", exact=True)).to_be_visible()
                for width in (1280, 1440, 1920):
                    await page.set_viewport_size({"width": width, "height": 1000})
                    await page.wait_for_function(
                        """width => {
                        const rect = document.querySelector('.model-reference-popup:popover-open')?.getBoundingClientRect();
                        return rect && rect.x >= 0 && rect.right <= width;
                    }""",
                        arg=width,
                    )
                    bounds = await popup.bounding_box()
                    assert bounds and bounds["x"] >= 0 and bounds["x"] + bounds["width"] <= width, (
                        width,
                        bounds,
                    )
                await page.set_viewport_size({"width": 1440, "height": 1000})
                if args.artifacts:
                    await page.screenshot(path=str(args.artifacts / "model-selector-desktop.png"))
                await popup.get_by_role("treeitem", name="official", exact=True).click()
                await expect(
                    popup.get_by_role("treeitem", name="model-a · official", exact=True)
                ).not_to_be_visible()
                await popup.get_by_role("searchbox").fill("third-party")
                await expect(
                    popup.get_by_role("treeitem", name="model-a · official", exact=True)
                ).to_have_count(0)
                await popup.get_by_role("treeitem", name="model-b · third-party", exact=True).click()
                model = page.get_by_label("主对话模型", exact=True)
                await expect(model).to_have_value("model-b")
                assert await model.locator("option:checked").inner_text() == "model-b · third-party"
                await model.press("ArrowDown")
                await popup.get_by_role("searchbox").fill("no-such-model")
                await expect(popup).to_contain_text("没有匹配的模型")
                await popup.get_by_role("searchbox").press("Escape")
                await expect(popup).not_to_be_visible()
                await expect(model).to_be_focused()
                await model.press("Enter")
                await popup.get_by_role("searchbox").fill("official")
                await popup.get_by_role("searchbox").press("ArrowDown")
                await page.keyboard.press("ArrowDown")
                await page.keyboard.press("Enter")
                await expect(page.get_by_label("主对话模型", exact=True)).to_have_value("model-a")
                # Same generic renderer supports task auto mode and recommendation drawers.
                await page.evaluate('showInactive = true; moveTo("tasks")')
                await page.get_by_role("button", name="放弃并切换", exact=True).click()
                defaults = page.get_by_label("默认任务模型", exact=True)
                await expect(defaults).to_have_value("auto")
                await defaults.click()
                await popup.get_by_role("treeitem", name="跟随主对话模型", exact=True).click()
                await expect(defaults).to_have_value("auto")
                await page.evaluate('openTaskRecommendation("fast")')
                recommendation = page.get_by_label("推荐模型", exact=True)
                await recommendation.click()
                await popup.get_by_role("searchbox").fill("model-b")
                assert await page.evaluate("taskRecommendations().fast.reasoning_effort") == "high"
                await popup.get_by_role("searchbox").press("Escape")
                await expect(page.locator("#entry-dialog")).to_be_visible()
                await recommendation.click()
                await popup.get_by_role("treeitem", name="model-b · third-party", exact=True).click()
                await expect(page.get_by_label("推荐模型", exact=True)).to_have_value("model-b")
                assert await page.evaluate("taskRecommendations().fast.reasoning_effort") == "high"
                await page.evaluate("finishEntity(true)")
                await page.evaluate("""document.getElementById('documents').append(drawField({
                    path: ['plugin_settings', 'synthetic', 'models'], files: [],
                    schema: {type: 'array', title: '模型列表', items: {type: 'string', 'x-reference': 'model_api.entries'}},
                    value: ['model-a'], explicit: true
                }))""")
                models = page.get_by_label("模型列表 · 第 1 项", exact=True)
                await models.click()
                await popup.get_by_role("treeitem", name="model-b · third-party", exact=True).click()
                await expect(models).to_have_value("model-b")
                assert await page.evaluate(
                    'edits.get(key(["plugin_settings", "synthetic", "models"])).value'
                ) == ["model-b"]
                await page.evaluate(
                    'selected="models"; modelTab="usage"; drafts.clear(); edits.clear(); changed(["main_model"], "set", "missing-entry"); render()'
                )
                missing = page.get_by_label("主对话模型", exact=True)
                await expect(missing).to_have_value("missing-entry")
                assert (
                    await missing.locator("option:checked").inner_text() == "missing-entry · 未配置 Provider"
                )
                assert entry.read_bytes() == before
                assert not errors, errors
                await browser.close()
                print(
                    "PASS: grouped Provider labels; duplicate upstream identities; search, collapse, keyboard and Escape; task auto and drawer; search preserves reasoning; no configuration writes or inference"
                )
        finally:
            await console.close()


if __name__ == "__main__":
    asyncio.run(main())
