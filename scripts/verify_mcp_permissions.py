"""Desktop permissions acceptance with real Pi/MCP discovery; no inference or device calls."""

import argparse
import asyncio
import json
import os
from pathlib import Path
import socket
import sys
import tempfile

from playwright.async_api import async_playwright, expect

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
os.environ["PYTHONPATH"] = str(ROOT / "src")


def port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def main():
    args = argparse.ArgumentParser(description=__doc__)
    args.add_argument("--browser")
    args = args.parse_args()
    from assistant_runtime.app.management import BusinessProcess
    from assistant_runtime.core.configuration.console import Console
    from assistant_runtime.core.configuration.settings import ManagementSettings
    from assistant_runtime.core.integrations.settings import definition
    from assistant_runtime.platforms.executables import pi_executable

    pi, _ = pi_executable("pi")
    if not pi:
        raise SystemExit("Install the pinned Pi runtime before running this check")
    screenshots = Path(tempfile.mkdtemp(prefix="pa-permissions-layout-"))
    with tempfile.TemporaryDirectory(prefix="pa-mcp-permissions-") as folder:
        root = Path(folder) / "MCP 实例 with spaces"
        root.mkdir()
        entry = root / "config.toml"
        entry.write_text(
            'root="."\npi_binary='
            + json.dumps(str(pi))
            + """
plugins=["ai_usage","models","workfiles","pi","execution","integrations"]
main_model="synthetic"
main_reasoning_effort="auto"
[model_api]
enabled=true
[model_api.providers.local]
protocol="openai_responses"
base_url="http://127.0.0.1:1/v1"
key_env="SYNTHETIC_KEY"
enabled=true
[model_api.entries.synthetic]
provider="local"
model="synthetic"
reasoning_efforts=[]
"""
        )
        (root / ".env").write_text("SYNTHETIC_KEY=not-a-real-provider-key\n")
        mcp = root / "fixture.py"
        marker = root / "business-action.txt"
        mcp.write_text(
            """from pathlib import Path
from fastmcp import FastMCP
m=FastMCP("permissions-fixture")
@m.tool(annotations={"readOnlyHint": True})
def read_note():
    return "synthetic"
@m.tool()
def write_note():
    Path("""
            + repr(str(marker))
            + """).write_text("executed")
    return "done"
m.run(transport="stdio",show_banner=False)
"""
        )
        registry_path = root / "config/mcp/servers.json"
        registry_path.parent.mkdir(parents=True)
        original = {
            "revision": 1,
            "removed": [],
            "servers": {
                "remote": {
                    "revision": 1,
                    "definition": definition(
                        {
                            "transport": {
                                "type": "http",
                                "url": "https://example.invalid/private?token=hidden-url-value",
                                "headers": {"Authorization": "hidden-header-value"},
                            },
                            "approval": "reject",
                        }
                    ),
                },
                "service_with_a_long_name_for_layout": {
                    "revision": 1,
                    "definition": definition(
                        {
                            "enabled": False,
                            "approval": "reject",
                            "transport": {
                                "type": "stdio",
                                "command": "/private/" + "long-directory/" * 24 + "node",
                            },
                        }
                    ),
                },
            },
        }
        registry_path.write_text(json.dumps(original))
        settings = ManagementSettings(
            root, entry, host="127.0.0.1", port=port(), startup_seconds=30, shutdown_seconds=15
        )
        (root / "management.toml").write_text(
            "root="
            + json.dumps(str(root))
            + "\nentry="
            + json.dumps(str(entry))
            + '\nhost="127.0.0.1"\nport='
            + str(settings.port)
            + "\n"
        )
        business = BusinessProcess(settings)
        console = Console(business)
        errors = []
        try:
            await console.start()
            await business.start()
            assert business.outcome == "running", business.error
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(
                    **({"executable_path": args.browser} if args.browser else {}), headless=True
                )
                page = await browser.new_page(viewport={"width": 1440, "height": 1000})
                page.on("pageerror", lambda e: errors.append(str(e)))
                page.on("dialog", lambda dialog: dialog.accept())
                await page.goto(f"http://127.0.0.1:{settings.port}")
                await page.get_by_label("管理员密码", exact=True).fill(
                    (business.state / "admin-token").read_text().strip()
                )
                await page.get_by_role("button", name="登录", exact=True).click()
                await expect(page.locator("#editor")).to_be_visible()
                await page.get_by_role("button", name="权限管理", exact=True).click()
                remote = page.locator('.mcp-service[data-name="remote"]')
                await expect(remote).to_contain_text("禁止调用")
                await expect(page.locator("#resource-mcp")).to_have_count(0)
                for width in (1024, 1280, 1440, 1920):
                    await page.set_viewport_size({"width": width, "height": 1000})
                    result = await page.locator(".mcp-panel").evaluate("""panel => {
                        const table = panel.querySelector('table');
                        const headers = [...table.querySelectorAll('th')].map(n => n.getBoundingClientRect());
                        const rows = [...table.querySelectorAll('tbody tr')];
                        const aligned = rows.every(row => [...row.cells].every((cell, i) =>
                            Math.abs(cell.getBoundingClientRect().x - headers[i].x) < 1));
                        const contained = rows.every(row => [...row.cells].every(cell =>
                            cell.scrollWidth <= cell.clientWidth + 1));
                        const actionsFit = rows.every(row => {
                            const buttons = [...row.querySelectorAll('.mcp-row-actions button')];
                            const cell = row.lastElementChild.getBoundingClientRect();
                            return buttons.every((button, i) => {
                                const r = button.getBoundingClientRect();
                                return r.x >= cell.x && r.right <= cell.right &&
                                    (!i || r.x >= buttons[i-1].getBoundingClientRect().right);
                            });
                        });
                        const padding = rows.every(row => row.querySelector('.mcp-identity').getBoundingClientRect().x
                            - row.firstElementChild.getBoundingClientRect().x >= 20);
                        const badgeFits = [...table.querySelectorAll('.badge')].every(n =>
                            n.getBoundingClientRect().width < n.closest('td').getBoundingClientRect().width - 20);
                        return {aligned, contained, actionsFit, padding, badgeFits,
                            pageFits: document.documentElement.scrollWidth <= innerWidth};
                    }""")
                    assert all(result.values()), (width, result)
                    if width in (1280, 1920):
                        await page.locator(".mcp-panel").screenshot(
                            path=str(screenshots / f"permissions-{width}.png")
                        )
                await page.set_viewport_size({"width": 1440, "height": 1000})
                search = page.get_by_role("searchbox", name="搜索 MCP 名称或连接")
                await search.fill("remote")
                await expect(
                    page.locator('.mcp-service[data-name="service_with_a_long_name_for_layout"]')
                ).to_be_hidden()
                await search.fill("")
                await remote.get_by_role("button", name="编辑", exact=True).click()
                drawer = page.locator("#entry-dialog")
                self_url = drawer.get_by_label("服务地址", exact=True)
                await expect(self_url).to_have_value("")
                await expect(drawer).not_to_contain_text("hidden-header-value")
                for width, height in ((1024, 768), (1440, 900), (1920, 1080)):
                    await page.set_viewport_size({"width": width, "height": height})
                    for tab in ("连接设置", "调用权限", "工具与验证"):
                        await drawer.get_by_role("tab", name=tab, exact=True).click()
                        result = await drawer.evaluate("""drawer => {
                            const body = drawer.querySelector('#entry-body');
                            const heading = drawer.querySelector('.dialog-heading').getBoundingClientRect();
                            const footer = drawer.querySelector('.drawer-footer').getBoundingClientRect();
                            const content = body.getBoundingClientRect();
                            const fields = [...body.querySelectorAll('.mcp-field')].filter(n => n.getClientRects().length);
                            body.scrollTop = body.scrollHeight;
                            return {
                                independentScroll: drawer.scrollTop === 0 && body.scrollWidth <= body.clientWidth,
                                noOverlap: content.y >= heading.bottom && content.bottom <= footer.y + 1,
                                footerVisible: footer.bottom <= innerHeight && footer.y >= 0,
                                inset: fields.every(n => n.getBoundingClientRect().x >= content.x + 24),
                                controlsFit: fields.every(n => n.scrollWidth <= n.clientWidth),
                            };
                        }""")
                        assert all(result.values()), (width, height, tab, result)
                        await drawer.locator("#entry-body").evaluate("body => body.scrollTop = 0")
                        if width == 1440:
                            await drawer.screenshot(path=str(screenshots / f"drawer-{tab}.png"))
                await page.set_viewport_size({"width": 1440, "height": 1000})
                await drawer.get_by_role("tab", name="调用权限", exact=True).click()
                await drawer.get_by_label("服务调用策略", exact=True).select_option("approve")
                await page.get_by_role("button", name="取消编辑", exact=True).click()
                await expect(remote).to_contain_text("禁止调用")
                assert json.loads(registry_path.read_text()) == original
                await remote.get_by_role("button", name="编辑", exact=True).click()
                await drawer.get_by_role("tab", name="调用权限", exact=True).click()
                await drawer.get_by_label("服务调用策略", exact=True).select_option("approve")
                await page.get_by_role("button", name="保留修改", exact=True).click()
                await expect(remote).to_contain_text("未保存")
                await page.get_by_role("button", name="实例管理", exact=True).click()
                await expect(page.locator("#leave-dialog")).to_be_visible()
                await page.get_by_role("button", name="保存并切换", exact=True).click()
                await expect(page.locator("#leave-dialog")).not_to_be_visible(timeout=30000)
                assert (
                    json.loads(registry_path.read_text())["servers"]["remote"]["definition"]["transport"]
                    == original["servers"]["remote"]["definition"]["transport"]
                )
                await page.get_by_role("button", name="权限管理", exact=True).click()
                await expect(remote).to_contain_text("自动允许")
                await page.get_by_role("button", name="添加 MCP", exact=True).click()
                await drawer.get_by_label("服务名称", exact=True).fill("fixture")
                await drawer.get_by_label("可执行文件", exact=True).fill(sys.executable)
                await drawer.get_by_label("命令参数 JSON 数组", exact=True).fill(json.dumps([str(mcp)]))
                await drawer.screenshot(path=str(screenshots / "drawer-stdio.png"))
                await drawer.get_by_text("高级连接设置", exact=True).click()
                await expect(drawer.get_by_label("启动超时（秒）", exact=True)).to_be_visible()
                await drawer.get_by_label("工具超时（秒）", exact=True).scroll_into_view_if_needed()
                assert await drawer.evaluate("""drawer => {
                    const body = drawer.querySelector('#entry-body');
                    const footer = drawer.querySelector('.drawer-footer').getBoundingClientRect();
                    return body.scrollTop > 0 && drawer.scrollTop === 0 && body.scrollWidth <= body.clientWidth
                        && body.getBoundingClientRect().bottom <= footer.y + 1 && footer.bottom <= innerHeight;
                }""")
                await drawer.get_by_role("tab", name="调用权限", exact=True).click()
                await expect(drawer.get_by_label("服务调用策略", exact=True)).to_have_value("approve")
                await drawer.get_by_role("tab", name="工具与验证", exact=True).click()
                await drawer.get_by_role("button", name="验证连接并发现工具", exact=True).click()
                await expect(drawer.locator('.mcp-tool-row[data-tool="read_note"]')).to_be_visible(
                    timeout=30000
                )
                await expect(drawer.locator('.mcp-tool-row[data-tool="write_note"]')).to_be_visible()
                await drawer.get_by_label("工具规则 · read_note", exact=True).select_option("reject")
                await page.get_by_role("button", name="保留修改", exact=True).click()
                row = page.locator('.mcp-service[data-name="fixture"]')
                await expect(row).to_be_visible()
                assert "fixture" not in json.loads(registry_path.read_text())["servers"]
                # Reopening cannot lose opaque arguments staged earlier in the same draft.
                await row.get_by_role("button", name="编辑", exact=True).click()
                await drawer.get_by_role("tab", name="调用权限", exact=True).click()
                await drawer.get_by_label("工具范围", exact=True).select_option("selected")
                await drawer.get_by_label("指定工具（每行一个）", exact=True).fill("read_note")
                await drawer.get_by_role("tab", name="工具与验证", exact=True).click()
                await drawer.get_by_label("工具规则 · write_note", exact=True).select_option("approve")
                await drawer.get_by_role("button", name="验证连接并发现工具", exact=True).click()
                await expect(drawer.locator('.mcp-tool-row[data-tool="read_note"]')).to_contain_text(
                    "工具被禁止", timeout=30000
                )
                await expect(drawer.locator('.mcp-tool-row[data-tool="write_note"]')).to_contain_text("允许")
                await page.get_by_role("button", name="保留修改", exact=True).click()
                await page.get_by_role("button", name="保存并生效", exact=True).click()
                await expect(page.locator("#notice")).to_contain_text("保存成功", timeout=30000)
                saved = json.loads(registry_path.read_text())["servers"]["fixture"]["definition"]
                assert saved["transport"]["args"] == [str(mcp)]
                assert saved["tool_approvals"] == {"read_note": "reject", "write_note": "approve"}
                assert set(saved["allow_tools"]) == {"read_note", "write_note"}
                assert not marker.exists()
                await expect(row).to_contain_text("已应用")
                await row.get_by_role("button", name="移除", exact=True).click()
                await expect(row).to_have_count(0)
                await page.get_by_role("button", name="保存并生效", exact=True).click()
                await expect(page.locator("#notice")).to_contain_text("保存成功", timeout=30000)
                assert "fixture" in json.loads(registry_path.read_text())["removed"]
                assert mcp.exists() and not marker.exists()
                assert not errors, errors
                await browser.close()
            print(
                "PASS: desktop MCP CRUD, shared save/apply, opaque retention, real Pi discovery, effective policy, removal; no inference or business-tool execution"
            )
            print(
                "PASS: 1024/1280/1440/1920 table alignment, padding, bounded paths, buttons and search; screenshots:",
                screenshots,
            )
        finally:
            await console.close()
            if business.apply_task:
                await asyncio.gather(business.apply_task, return_exceptions=True)
            await business.stop()


if __name__ == "__main__":
    asyncio.run(main())
