"""Isolated configuration UI and image-resource acceptance; no models or live service."""

import argparse
import asyncio
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
                assert not errors, errors
                await browser.close()
                print(
                    "PASS: appearance forms; upload normalized on server; no files before save; atomic resource save; authenticated PNG preview and ETag; restore/cancel; no model calls"
                )
        finally:
            await console.close()


if __name__ == "__main__":
    asyncio.run(main())
