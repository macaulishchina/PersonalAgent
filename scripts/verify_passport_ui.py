"""Run PP browser acceptance against isolated management and worker processes."""

import argparse
import asyncio
import ast
import json
import re
import tempfile
import tomllib
import uuid
from pathlib import Path
from aiohttp import ClientSession, web
from aiohttp.test_utils import TestServer
from playwright.async_api import async_playwright, expect

from assistant_runtime.app.management import BusinessProcess
from assistant_runtime.core.configuration.console import Console
from assistant_runtime.core.configuration.settings import ManagementSettings

ROOT = Path(__file__).resolve().parents[1]


def provisioning_html():
    source = (
        ROOT / "src/assistant_runtime/plugins/ai_passport/firmware/apps/pa_terminal/main/provision.c"
    ).read_text()
    body = source.split("static const char html[] =", 1)[1].split("httpd_resp_set_type", 1)[0]
    return "".join(ast.literal_eval(value) for value in re.findall(r'"(?:\\.|[^"\\])*"', body))


async def verify_management(browser, screenshot=None):
    with tempfile.TemporaryDirectory(prefix="pa-passport-ui-") as folder:
        root = Path(folder)
        entry = root / "config.toml"
        entry.write_text(
            f"root = {json.dumps(str(root))}\n"
            'plugins = ["ai_usage", "models", "workfiles", "pi", "execution", "conversation", "speech_io", "ai_passport"]\n'
            "[plugin_settings.ai_passport]\n",
            encoding="utf-8",
        )
        business = BusinessProcess(ManagementSettings(root, entry, host="127.0.0.1", port=0))
        console = Console(business)
        await business.start()
        try:
            assert business.outcome == "running", business.error
            await console.start()
            origin = f"http://127.0.0.1:{console.site._server.sockets[0].getsockname()[1]}"
            page = await browser.new_page(viewport={"width": 1440, "height": 1000})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            device_id = uuid.uuid4().hex
            async with ClientSession() as device:
                async with device.post(
                    origin + "/entrances/ai_passport/pair",
                    json={"device_id": device_id, "credential": "d" * 64, "protocol": 2},
                ) as response:
                    assert response.status == 200, await response.text()
                    challenge = await response.json()

            await page.goto(origin + "/config/")
            await page.locator("#token").fill((business.state / "admin-token").read_text().strip())
            await page.locator("#login button").click()
            await page.get_by_role("button", name="AI Passport", exact=True).click()
            frame = page.frame_locator("iframe.plugin-management-frame")
            await expect(frame.get_by_role("heading", name="已配对设备")).to_be_visible()
            await frame.get_by_role("button", name="配对新设备", exact=True).click()
            await frame.get_by_label("设备名称").fill("客厅 PP")
            invalid_code = "00000000" if challenge["code"] != "00000000" else "00000001"
            await frame.get_by_label("配对码").fill(invalid_code)
            page.once("dialog", lambda dialog: dialog.accept())
            await frame.get_by_role("button", name="确认配对").click()
            await expect(frame.get_by_role("status")).to_contain_text("配对码无效或已过期")
            await frame.get_by_label("配对码").fill(challenge["code"])
            page.once("dialog", lambda dialog: dialog.accept())
            await frame.get_by_role("button", name="确认配对").click()
            await expect(frame.get_by_role("status")).to_contain_text("配对成功")
            await expect(frame.get_by_text("客厅 PP", exact=True)).to_be_visible()
            if screenshot:
                screenshot.parent.mkdir(parents=True, exist_ok=True)
                await page.screenshot(path=str(screenshot), full_page=True)

            # A page-declared write cannot enter through the old unversioned endpoint.
            response = await page.request.post(
                origin + "/api/configuration-action",
                data={
                    "owner": "ai_passport",
                    "action": "revoke",
                    "confirmed": True,
                    "arguments": {"device_id": device_id},
                },
                headers={"Origin": origin},
            )
            assert response.status == 400 and "version_required" in await response.text()

            await frame.get_by_role("button", name="默认设置", exact=True).click()
            await frame.get_by_label("初始音量", exact=True).fill("60")
            await page.get_by_role("button", name="模型与账号", exact=True).click()
            await page.get_by_role("button", name="AI Passport", exact=True).click()
            await frame.get_by_role("button", name="设备配置", exact=True).click()
            volume = frame.get_by_label("初始音量", exact=True)
            await expect(volume).to_have_value("60")
            await volume.fill("85")
            await frame.get_by_role("button", name="添加 Wi-Fi").click()
            await frame.get_by_label("新 Wi-Fi 名称").fill("Private test")
            await frame.get_by_role("button", name="添加", exact=True).click()
            await frame.get_by_label("Wi-Fi 密码", exact=True).fill("synthetic-password")
            await expect(page.locator("#count")).to_have_text("4 项未保存")
            pid = (await business.status())["pid"]
            await page.locator("#save").click()
            await expect(page.locator("#count")).to_have_text("没有未保存的修改", timeout=20000)
            await expect(frame.get_by_role("heading", name="客厅 PP")).to_be_visible()
            values = tomllib.loads(entry.read_text())["plugin_settings"]["ai_passport"]
            assert values["volume"] == 60
            settings = values["devices"][device_id]
            assert settings["volume"] == 85
            assert settings["wifi_networks"]["1"] == {
                "ssid": "Private test",
                "password": "synthetic-password",
            }
            assert (await business.status())["pid"] == pid
            assert (
                "synthetic-password"
                not in await (await page.request.get(origin + "/api/configuration")).text()
            )
            await expect(frame.get_by_label("Wi-Fi 密码", exact=True)).to_have_value("")

            # Remove the last network and add a different one in the same draft.
            await frame.get_by_role("button", name="移除", exact=True).click()
            await frame.get_by_role("button", name="添加 Wi-Fi").click()
            await frame.get_by_label("新 Wi-Fi 名称").fill("Replacement")
            await frame.get_by_role("button", name="添加", exact=True).click()
            await frame.get_by_role("button", name="沿用默认", exact=True).click()
            await page.locator("#save").click()
            await expect(page.locator("#count")).to_have_text("没有未保存的修改", timeout=20000)
            stored = tomllib.loads(entry.read_text())["plugin_settings"]["ai_passport"]["devices"][device_id]
            assert stored == {"wifi_networks": {"1": {"ssid": "Replacement"}}}, stored
            await expect(volume).to_have_value("60")
            await expect(frame.get_by_role("button", name="撤销配对")).to_be_enabled()
            page.once("dialog", lambda dialog: dialog.accept())
            await frame.get_by_role("button", name="撤销配对").click()
            await expect(frame.get_by_text("还没有配对设备。")).to_be_visible()
            assert not errors, errors
            await page.close()
        finally:
            await console.close()
            await business.stop()


async def verify_bootstrap(browser):
    # Extract the actual firmware's bootstrap form and supply a synthetic device endpoint.
    app = web.Application()
    submitted = []

    async def setup(_):
        return web.Response(text=provisioning_html(), content_type="text/html")

    async def configure(request):
        submitted.append(await request.json())
        return web.Response(text="已保存")

    app.router.add_get("/setup", setup)
    app.router.add_post("/configure", configure)
    async with TestServer(app) as server:
        page = await browser.new_page()
        await page.goto(str(server.make_url("/setup")))
        await page.get_by_label("Wi-Fi 名称", exact=True).fill("Synthetic Home")
        await page.get_by_label("Wi-Fi 密码", exact=True).fill("synthetic-password")
        await page.get_by_label("PA 业务地址", exact=True).fill("http://example.test:8047")
        await page.get_by_role("button", name="保存并连接", exact=True).click()
        await expect(page.locator("#status")).to_contain_text("八位配对码")
        assert submitted == [
            {
                "ssid": "Synthetic Home",
                "password": "synthetic-password",
                "server_url": "http://example.test:8047",
                "trusted_ca": "",
            }
        ]
        assert not await page.locator("#password").input_value()
        assert await page.evaluate("localStorage.length + sessionStorage.length") == 0
        await page.close()


async def verify(screenshot=None):
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        try:
            await verify_management(browser, screenshot)
            await verify_bootstrap(browser)
        finally:
            await browser.close()
    print(
        "PASS: bundled PP page, real admin pairing and live apply, scoped device drafts, firmware form (synthetic)"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--screenshot", type=Path)
    args = parser.parse_args()
    asyncio.run(verify(args.screenshot))
