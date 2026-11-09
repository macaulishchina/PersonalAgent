"""Installed plugin page acceptance with a real isolated manager/worker, no external calls.

Install the built examples/configuration-plugin wheel in an isolated target on PYTHONPATH.
Run with the current framework source or a framework wheel; no repository paths are used below.
"""

import argparse
import asyncio
import json
from pathlib import Path
import tempfile
import tomllib

from playwright.async_api import async_playwright, expect
from assistant_runtime.app.management import BusinessProcess
from assistant_runtime.core.configuration.console import Console
from assistant_runtime.core.configuration.settings import ManagementSettings
from assistant_runtime.core.configuration.pages import check


async def verify(browser_path=None):
    assert not check(), check()
    with tempfile.TemporaryDirectory(prefix="pa-plugin-page-") as folder:
        root = Path(folder)
        entry = root / "config.toml"
        entry.write_text(
            f'root={json.dumps(str(root))}\nplugins=["configuration_demo"]\n[plugin_settings.configuration_demo]\n'
        )
        business = BusinessProcess(
            ManagementSettings(root, entry, host="127.0.0.1", port=0, startup_seconds=15)
        )
        console = Console(business)
        await business.start()
        try:
            assert business.outcome == "running", business.error
            await console.start()
            port = console.site._server.sockets[0].getsockname()[1]
            origin = f"http://127.0.0.1:{port}"
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(
                    **({"executable_path": browser_path} if browser_path else {})
                )
                try:
                    page = await browser.new_page(viewport={"width": 1440, "height": 1000})
                    errors = []
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    await page.goto(origin + "/config/")
                    await page.locator("#token").fill((business.state / "admin-token").read_text().strip())
                    await page.locator("#login button").click()
                    await page.get_by_role("button", name="页面开发示例", exact=True).click()
                    frame = page.frame_locator("iframe.plugin-management-frame")
                    await expect(frame.get_by_role("heading", name="基础配置")).to_be_visible()
                    label = frame.get_by_label("显示名称", exact=True)
                    await label.fill("独立页面验收")
                    await label.press("Tab")
                    await expect(page.locator("#count")).to_contain_text("未保存")
                    await page.get_by_role("button", name="模型与账号", exact=True).click()
                    await page.get_by_role("button", name="页面开发示例", exact=True).click()
                    await expect(frame.get_by_label("显示名称", exact=True)).to_have_value("独立页面验收")
                    # Custom control and resources join the same draft.
                    await frame.get_by_label("目标标识").fill("desktop")
                    await frame.get_by_role("button", name="添加目标").click()
                    await expect(frame.get_by_label("desktop 名称")).to_be_visible()
                    await frame.get_by_label("私有口令", exact=True).fill("synthetic-secret-never-return")
                    await frame.get_by_label("私有口令", exact=True).press("Tab")
                    await frame.get_by_label("说明文档", exact=True).fill("独立插件资源")
                    await frame.get_by_label("说明文档", exact=True).press("Tab")
                    await frame.get_by_label("资源凭据", exact=True).fill("synthetic-resource-secret")
                    await frame.get_by_label("资源凭据", exact=True).press("Tab")
                    await frame.get_by_label("规则", exact=True).fill('{"allow":true}')
                    await frame.get_by_label("规则", exact=True).press("Tab")
                    await page.locator("#save").click()
                    await expect(page.locator("#count")).to_have_text("没有未保存的修改", timeout=20000)
                    stored = tomllib.loads(entry.read_text())["plugin_settings"]["configuration_demo"]
                    assert stored["label"] == "独立页面验收", stored
                    assert stored["secret"] == "synthetic-secret-never-return"
                    assert (
                        root / "config/plugins/configuration_demo/notes.txt"
                    ).read_text() == "独立插件资源"
                    body = await (await page.request.get(origin + "/api/configuration")).text()
                    assert "synthetic-secret-never-return" not in body
                    assert "synthetic-resource-secret" not in body
                    assert (
                        root / "config/plugins/configuration_demo/token.txt"
                    ).read_text() == "synthetic-resource-secret"
                    assert "synthetic-secret-never-return" not in (business.state / "audit.jsonl").read_text()
                    await frame.get_by_role("button", name="查询运行状态").click()
                    await expect(frame.locator("[role=status]")).to_contain_text("独立页面验收")
                    await frame.get_by_role("button", name="下一步").click()
                    page.once("dialog", lambda dialog: dialog.accept())
                    await frame.get_by_role("button", name="确认登记").click()
                    await expect(frame.locator("[role=status]")).to_contain_text("true")
                    # Defaults delete explicit overrides; credential clearing has distinct semantics.
                    await (
                        frame.get_by_label("显示名称", exact=True)
                        .locator("..")
                        .get_by_role("button", name="恢复默认")
                        .click()
                    )
                    await expect(frame.get_by_label("显示名称", exact=True)).to_have_value("我的插件")
                    await frame.get_by_role("button", name="清除私有口令", exact=True).click()
                    await frame.get_by_role("button", name="清除资源凭据", exact=True).click()
                    await page.locator("#save").click()
                    await expect(page.locator("#count")).to_have_text("没有未保存的修改", timeout=20000)
                    stored = tomllib.loads(entry.read_text())["plugin_settings"]["configuration_demo"]
                    assert "label" not in stored and "secret" not in stored, stored
                    assert not (root / "config/plugins/configuration_demo/token.txt").exists()
                    # Conditional constraints reach the page, not only a global toast.
                    await frame.get_by_label("连接类型", exact=True).select_option("remote")
                    await page.locator("#save").click()
                    await expect(frame.locator("[role=status]")).to_contain_text("过短")
                    await frame.get_by_label("远端地址", exact=True).fill("https://example.invalid")
                    await frame.get_by_label("远端地址", exact=True).press("Tab")
                    await page.locator("#save").click()
                    await expect(page.locator("#count")).to_have_text("没有未保存的修改", timeout=20000)
                    # Business-down editing remains available and actions diagnose failure.
                    await business.stop()
                    await page.locator("#refresh").click()
                    await expect(page.locator("#health")).to_have_attribute("data-state", "stopped")
                    await expect(frame.get_by_role("heading", name="基础配置")).to_be_visible()
                    await frame.get_by_role("button", name="查询运行状态").click()
                    await expect(frame.locator("[role=status]")).to_contain_text("未启动")
                    # A disabled plugin still has its page and semantic validation.
                    console.files.patch(
                        console.files.snapshot().version,
                        [{"path": ["plugins"], "op": "set", "value": []}],
                    )
                    await page.reload()
                    await page.get_by_role("button", name="页面开发示例", exact=True).click()
                    await expect(frame.get_by_label("远端地址", exact=True)).to_have_value(
                        "https://example.invalid"
                    )
                    await frame.get_by_label("远端地址", exact=True).fill("invalid-address")
                    await page.locator("#save").click()
                    await expect(frame.locator("[role=status]")).to_contain_text("HTTP")
                    await frame.get_by_label("远端地址", exact=True).fill("https://other.invalid")
                    await page.locator("#save").click()
                    await expect(page.locator("#count")).to_have_text("没有未保存的修改", timeout=20000)
                    assert not errors, errors
                    print(
                        "PASS: installed page, cross-page draft, dynamic object, credentials, resources, hot apply, actions, conditional validation, defaults/secret clearing, business-down and disabled editing"
                    )
                finally:
                    await browser.close()
        finally:
            await console.close()
            await business.stop()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--browser")
    args = parser.parse_args()
    asyncio.run(verify(args.browser))
