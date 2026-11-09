"""Check all bundled extension pages in Chromium against an isolated installed PA."""

import asyncio
import json
from pathlib import Path
import tempfile

from playwright.async_api import async_playwright, expect

from assistant_runtime.app.management import BusinessProcess
from assistant_runtime.core.configuration.console import Console
from assistant_runtime.core.configuration.settings import ManagementSettings
from assistant_runtime.core.plugin_catalog import OWNERS


async def main():
    with tempfile.TemporaryDirectory(prefix="pa-plugin-navigation-") as folder:
        root = Path(folder)
        entry = root / "config.toml"
        entry.write_text(f"root = {json.dumps(str(root))}\nplugins = []\n", encoding="utf-8")
        business = BusinessProcess(ManagementSettings(root, entry, host="127.0.0.1", port=0))
        console = Console(business)
        await console.start()
        try:
            port = console.site._server.sockets[0].getsockname()[1]
            origin = f"http://127.0.0.1:{port}"
            async with async_playwright() as pw:
                browser = await pw.chromium.launch()
                try:
                    page = await browser.new_page(viewport={"width": 1440, "height": 1000})
                    errors = []
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    await page.goto(origin + "/config/")
                    await page.locator("#token").fill((business.state / "admin-token").read_text().strip())
                    await page.locator("#login button").click()
                    await expect(page.locator("#editor")).to_be_visible()
                    await page.reload()
                    await expect(page.locator("#health")).to_have_text("业务已停止")
                    # An authenticated reload fetches status twice before starting the poll.
                    status_reads = """() => performance.getEntriesByName(
                        new URL("/api/status", location.href).href
                    ).length"""
                    await page.wait_for_function(f"() => ({status_reads})() >= 2")
                    await expect(page.locator("#notice")).to_be_empty()
                    await page.wait_for_function(f"() => ({status_reads})() >= 3", timeout=8000)
                    await expect(page.locator("#health")).to_have_text("业务已停止")
                    catalog = await (await page.request.get(origin + "/api/catalog")).json()
                    owners = {ident for ident, module in OWNERS.items() if ".plugins." in module}
                    for plugin in catalog["plugins"]:
                        ident = plugin["id"]
                        if ident not in owners:
                            continue
                        spec = plugin["configuration"]
                        assert spec["namespace"] == "plugin_settings." + ident, ident
                        assert plugin["page"] == "plugin:" + ident, ident
                        await (
                            page.get_by_role("navigation", name="配置分类")
                            .get_by_role("button", name="功能与插件", exact=True)
                            .click()
                        )
                        card = page.locator(".plugin-option").filter(
                            has=page.locator(f'input[value="{ident}"]')
                        )
                        await card.get_by_role("button", name="配置", exact=True).click()
                        await expect(page.locator("#page-title")).to_have_text(spec["title"])
                        documents = page.locator("#documents")
                        await expect(documents.get_by_role("button", name="返回功能与插件")).to_be_visible()
                        sections = {
                            value["section"]
                            for value in spec.get("interface", {}).get("fields", {}).values()
                            if "section" in value
                        }
                        for section in sections:
                            await expect(
                                documents.get_by_role("heading", name=section, exact=True)
                            ).to_be_visible()
                        await page.set_viewport_size({"width": 1024, "height": 800})
                        assert await page.evaluate(
                            "document.documentElement.scrollWidth <= window.innerWidth"
                        ), ident
                        await page.set_viewport_size({"width": 1440, "height": 1000})
                        for link in spec.get("interface", {}).get("links", []):
                            await expect(
                                page.locator("#documents").get_by_role(
                                    "button", name=link["title"], exact=True
                                )
                            ).to_be_visible()
                        if ident == "web":
                            await expect(page.get_by_role("button", name="打开聊天界面")).to_be_disabled()
                            await expect(page.get_by_label("启用", exact=True)).to_be_visible()
                            address = await page.get_by_role("link", name="会话快捷链接").get_attribute(
                                "href"
                            )
                            async with page.expect_popup() as popup:
                                await page.get_by_role("link", name="会话快捷链接").click()
                            linked = await popup.value
                            await expect(linked.get_by_role("heading", name="会话快捷链接")).to_be_visible()
                            assert address.startswith("/api/plugin-pages/web/assets/")
                            script = await page.request.get(origin + address.replace(".html", ".js"))
                            assert script.ok
                            css = await page.request.get(origin + address.replace(".html", ".css"))
                            assert css.ok and "text/css" in css.headers["content-type"]
                            await linked.close()
                        elif ident == "ai_passport":
                            frame = page.frame_locator("iframe.plugin-management-frame")
                            await expect(frame.get_by_role("button", name="默认设置")).to_be_visible()
                        elif ident == "go2rtc":
                            await expect(page.get_by_role("button", name="重启全部网关")).to_be_visible()
                            await page.get_by_role("button", name="管理设备", exact=True).click()
                            await expect(page.locator("#page-title")).to_have_text("连接与设备")
                        owners.remove(ident)
                    assert not owners, owners
                    assert not errors, errors
                    print(
                        "PASS: all bundled extension pages, scoped Web link, PP iframe and shared device navigation"
                    )
                finally:
                    await browser.close()
        finally:
            await console.close()
            await business.stop()


if __name__ == "__main__":
    asyncio.run(main())
