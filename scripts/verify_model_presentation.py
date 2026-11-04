"""Isolated configuration UI and image-resource acceptance; no models or live service."""

import argparse
import asyncio
import html
from io import BytesIO
from pathlib import Path
import socket
import tempfile
from unittest.mock import AsyncMock

from PIL import Image
from playwright.async_api import async_playwright, expect
from assistant_runtime.app.management import BusinessProcess
from assistant_runtime.core.configuration.console import Console
from assistant_runtime.core.configuration.settings import ManagementSettings


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--browser", required=True)
    parser.add_argument("--artifacts", type=Path)
    args = parser.parse_args()
    if args.artifacts:
        args.artifacts.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="pa-model-appearance-") as folder:
        root = Path(folder)
        entry = root / "config.toml"
        entry.write_text(
            'root="."\nplugins=[]\n[model_api.providers.test]\npreset="deepseek"\nenabled=false\n[model_api.entries.model-a]\nprovider="test"\nmodel="synthetic"\nreasoning_efforts=["low"]\n',
            encoding="utf-8",
        )
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        business = BusinessProcess(ManagementSettings(root, entry, host="127.0.0.1", port=port))
        console = Console(business)
        console.provider_queries.references.download = AsyncMock(side_effect=OSError("offline"))
        await console.start()
        try:
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(headless=True, executable_path=args.browser)
                page = await browser.new_page(viewport={"width": 1280, "height": 900})
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                await page.goto(f"http://127.0.0.1:{port}/config/", wait_until="domcontentloaded")
                await page.get_by_label("管理员密码", exact=True).fill(
                    (business.state / "admin-token").read_text().strip()
                )
                await page.get_by_role("button", name="登录", exact=True).click()
                await expect(page.locator("#editor")).to_be_visible()
                await page.get_by_role("button", name="模型与账号", exact=True).click()
                await page.wait_for_function("Boolean(snapshot?.fields)")
                await page.evaluate("openEntity('providers','test')")
                appearance = page.locator(".model-presentation")
                await expect(appearance).to_be_visible()
                await appearance.get_by_label("公开名称", exact=True).fill("Synthetic Team")
                await appearance.get_by_label("简短说明", exact=True).fill("Public metadata only")
                await expect(appearance.get_by_text("浏览内置头像 · 71 款", exact=True)).to_be_visible()
                await appearance.get_by_text("浏览内置头像 · 71 款", exact=True).click()
                await expect(appearance.locator(".model-avatar-choice")).to_have_count(71)
                await appearance.get_by_label("搜索头像", exact=True).fill("deepseek")
                await appearance.get_by_role("button", name="DeepSeek 深度求索", exact=True).click()
                await expect(appearance.get_by_label("头像", exact=True)).to_have_value("builtin:deepseek")
                await expect(appearance.get_by_alt_text("模型头像预览")).to_have_js_property(
                    "naturalWidth", 64
                )
                await appearance.get_by_text("浏览内置头像 · 71 款", exact=True).click()
                await appearance.get_by_label("搜索头像", exact=True).fill("")
                if args.artifacts:
                    await appearance.screenshot(path=str(args.artifacts / "builtin-avatar-library.png"))
                await appearance.get_by_label("搜索头像", exact=True).fill("小猫")
                await appearance.get_by_role("button", name="小猫", exact=True).click()
                await expect(appearance.get_by_label("头像", exact=True)).to_have_value("builtin:cat")
                raw = BytesIO()
                Image.new("RGB", (600, 400), "blue").save(raw, format="PNG")
                await appearance.get_by_label("上传模型头像", exact=True).set_input_files(
                    {"name": "avatar.png", "mimeType": "image/png", "buffer": raw.getvalue()}
                )
                await expect(
                    appearance.get_by_text("图片已校验，保存配置后生效。", exact=True)
                ).to_be_visible()
                assert not (root / "config/model-avatars").exists(), "Upload must not apply configuration"
                await expect(appearance.get_by_alt_text("模型头像预览")).to_be_visible()
                await expect(appearance.get_by_alt_text("模型头像预览")).to_have_js_property(
                    "naturalWidth", 256
                )
                if args.artifacts:
                    await appearance.screenshot(path=str(args.artifacts / "provider-avatar-draft.png"))
                payload = await page.evaluate(
                    """() => { activeEditor.commit?.(); return {version:snapshot.version,patches:[...edits.values()],resources:[...resourceEdits.values()]}; }"""
                )
                assert payload["resources"] and payload["patches"]
                before = entry.read_bytes()
                # Exercise exactly the server transaction used by save, without starting business.
                await asyncio.to_thread(
                    business.files.patch,
                    payload["version"],
                    payload["patches"],
                    resource_changes=payload["resources"],
                )
                assert entry.read_bytes() != before
                image_id = payload["resources"][0]["id"].removeprefix("model-avatar-")
                response = await page.request.get(
                    f"http://127.0.0.1:{port}/api/model-avatar/{image_id}?version={image_id}"
                )
                assert response.status == 200
                assert response.headers["content-type"] == "image/png"
                assert response.headers["x-content-type-options"] == "nosniff"
                assert len(await response.body()) <= 256 * 1024
                cached = await page.request.get(
                    f"http://127.0.0.1:{port}/api/model-avatar/{image_id}?version={image_id}",
                    headers={"If-None-Match": response.headers["etag"]},
                )
                assert cached.status == 304
                await page.reload(wait_until="domcontentloaded")
                await expect(page.locator("#editor")).to_be_visible()
                await page.get_by_role("button", name="模型与账号", exact=True).click()
                await page.wait_for_function("Boolean(snapshot?.fields)")
                await page.evaluate("openEntity('providers','test')")
                appearance = page.locator(".model-presentation")
                await expect(appearance.get_by_label("公开名称", exact=True)).to_have_value("Synthetic Team")
                await expect(appearance.get_by_alt_text("模型头像预览")).to_be_visible()
                await expect(appearance.get_by_alt_text("模型头像预览")).to_have_js_property(
                    "naturalWidth", 256
                )
                await appearance.get_by_role("button", name="恢复默认头像", exact=True).click()
                assert not await page.evaluate(
                    '[...resourceEdits.keys()].some(id=>id.startsWith("model-avatar-"))'
                )
                await page.locator("#entry-dialog").get_by_role("button", name="取消编辑", exact=True).click()
                await page.evaluate("openEntity('entries','model-a')")
                await expect(page.locator(".model-presentation")).to_be_visible()
                await (
                    page.locator(".model-presentation")
                    .get_by_label("公开名称", exact=True)
                    .fill("Synthetic Model")
                )
                await expect(page.locator('.model-presentation img[alt="模型头像预览"]')).to_have_js_property(
                    "naturalWidth", 256
                )
                await (
                    page.locator(".model-presentation")
                    .get_by_text("浏览内置头像 · 71 款", exact=True)
                    .click()
                )
                await page.locator(".model-presentation").get_by_label("搜索头像", exact=True).fill("机器人")
                await (
                    page.locator(".model-presentation")
                    .get_by_role("button", name="机器人", exact=True)
                    .click()
                )
                await expect(
                    page.locator(".model-presentation").get_by_label("头像", exact=True)
                ).to_have_value("builtin:robot")
                for width in (375, 768, 1280):
                    await page.set_viewport_size({"width": width, "height": 900})
                    await page.locator(".model-avatar-picker").evaluate("picker => picker.open = true")
                    bounds = await page.locator(".model-avatar-choices").evaluate(
                        "el => ({width:el.clientWidth,scroll:el.scrollWidth})"
                    )
                    assert bounds["scroll"] <= bounds["width"], bounds
                assert not errors, errors
                if args.artifacts:
                    directory = await (
                        await page.request.get(f"http://127.0.0.1:{port}/api/model-avatars")
                    ).json()
                    sections = []
                    for group, label in (("providers", "AI 提供商"), ("general", "通用头像")):
                        cards = "".join(
                            f'<div><img src="/api/model-avatar/builtin/{item["name"]}?version={item["version"]}"><span>{html.escape(item["label"])}</span></div>'
                            for item in directory["avatars"]
                            if item["group"] == group
                        )
                        sections.append(f"<h2>{label}</h2><section>{cards}</section>")
                    body = (
                        '<meta charset="utf-8"><style>body{font-family:Segoe UI,Microsoft YaHei,sans-serif;margin:32px;background:#f6f8f6;color:#24332d}h2{font-size:18px;margin:24px 0 16px}section{display:grid;grid-template-columns:repeat(10,1fr);gap:12px}section div{display:grid;justify-items:center;align-content:start;gap:8px;background:white;padding:14px 6px;border-radius:14px}img{width:48px;height:48px}span{font-size:11px;text-align:center}</style>'
                        + "".join(sections)
                    )
                    sheet = page
                    await sheet.set_viewport_size({"width": 1280, "height": 1100})
                    await sheet.route(
                        "**/avatar-preview.html",
                        lambda route: route.fulfill(body=body, content_type="text/html"),
                    )
                    await sheet.goto(f"http://127.0.0.1:{port}/avatar-preview.html", wait_until="networkidle")
                    assert await sheet.locator("img").evaluate_all(
                        "imgs => imgs.length === 71 && imgs.every(img => img.complete && img.naturalWidth === 64)"
                    )
                    await sheet.screenshot(
                        path=str(args.artifacts / "builtin-avatar-contact-sheet.png"), full_page=True
                    )
                await browser.close()
                print(
                    "PASS: 30 brand + 40 general avatars; visual search/select; inherited preview; mobile layout; upload normalization and atomic save; authenticated PNG/ETag; restore/cancel; no model calls"
                )
        finally:
            await console.close()


if __name__ == "__main__":
    asyncio.run(main())
