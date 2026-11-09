"""PC pairing controls against synthetic administrator replies; no device or paid requests."""

import argparse
import asyncio
import ast
import json
import re
from pathlib import Path
from aiohttp import web
from aiohttp.test_utils import TestServer
from playwright.async_api import async_playwright, expect

from assistant_runtime.core.configuration.fields import described_schema, public_rule

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "src/assistant_runtime/core/configuration/static"


def provisioning_html():
    source = (
        ROOT / "src/assistant_runtime/plugins/ai_passport/firmware/apps/pa_terminal/main/provision.c"
    ).read_text()
    body = source.split("static const char html[] =", 1)[1].split("httpd_resp_set_type", 1)[0]
    return "".join(ast.literal_eval(value) for value in re.findall(r'"(?:\\.|[^"\\])*"', body))


async def verify(screenshot=None):
    app = web.Application()
    submitted = []

    descriptor = json.loads(
        (ROOT / "src/assistant_runtime/plugins/ai_passport/configuration.json").read_text()
    )
    device_id = "test-device"
    devices = [
        {
            "name": "PP-Preview-1",
            "device_id": device_id,
            "session_id": "test-session",
            "paired": True,
            "connected": True,
            "configuration": {"volume": {"desired": 2, "applied": 1}},
        },
        {"name": "旧设备", "device_id": "revoked", "paired": True, "revoked": True},
        {"name": "尚未配对", "device_id": "unpaired", "paired": False},
    ]
    operations, saved = [], []
    fields = [{"path": ["plugins"], "schema": {}, "value": ["ai_passport"], "group": "plugins"}]
    for name, rule in described_schema(descriptor)["properties"].items():
        fields.append(
            {
                "path": ["ai_passport", name],
                "schema": public_rule(rule),
                "page": "plugin:ai_passport",
                "owner_id": "ai_passport",
                "files": ["passport.toml"],
                "value": {} if name == "devices" else rule.get("default"),
                "explicit": False,
            }
        )

    async def status(_):
        return web.json_response({"loaded_version": "test-version", "state": "running"})

    async def catalog(_):
        return web.json_response(
            {
                "pages": [{"id": "plugin:ai_passport", "title": "AI Passport"}],
                "plugins": [{"id": "ai_passport", "page": "plugin:ai_passport", "configuration": descriptor}],
            }
        )

    async def configuration(request):
        if request.method == "POST":
            saved.append(await request.json())
            return web.json_response({"version": "test-version"})
        return web.json_response({"fields": fields, "resources": [], "version": "test-version"})

    async def entrance(request):
        body = await request.json()
        operations.append(body)
        if body["action"] == "list":
            return web.json_response({"devices": devices})
        if body["action"] == "revoke":
            devices[0]["revoked"] = True
        return web.json_response({})

    async def home(_):
        return web.FileResponse(STATIC / "index.html")

    async def setup(_):
        return web.Response(text=provisioning_html(), content_type="text/html")

    app.router.add_get("/", home)
    app.router.add_get("/setup", setup)
    app.router.add_get("/api/status", status)
    app.router.add_get("/api/catalog", catalog)
    app.router.add_route("*", "/api/configuration", configuration)
    app.router.add_post("/api/entrances/ai_passport", entrance)

    async def configure(request):
        submitted.append(await request.json())
        return web.Response(text="已保存")

    app.router.add_post("/configure", configure)
    app.router.add_static("/", STATIC)
    server = TestServer(app)
    await server.start_server()
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            try:
                page = await browser.new_page(viewport={"width": 1440, "height": 1000})
                errors = []
                page.on("pageerror", lambda e: errors.append(str(e)))
                page.on("dialog", lambda d: asyncio.create_task(d.accept()))
                await page.goto(str(server.make_url("/")))
                await page.get_by_role("button", name="AI Passport", exact=True).click()
                await expect(page.get_by_text("PP-Preview-1")).to_be_visible()
                await expect(page.get_by_text("1 项待设备确认")).to_be_visible()
                await expect(page.get_by_text("旧设备")).to_have_count(0)
                await expect(page.get_by_text("尚未配对")).to_have_count(0)
                await expect(page.get_by_label("配对码", exact=True)).to_be_hidden()
                if screenshot:
                    screenshot.parent.mkdir(parents=True, exist_ok=True)
                    await page.screenshot(path=str(screenshot), full_page=True)

                await page.get_by_role("button", name="默认设置", exact=True).click()
                await expect(page.get_by_label("初始音量", exact=True)).to_have_value("45")
                await page.get_by_label("初始音量", exact=True).fill("60")
                await page.get_by_role("button", name="设备", exact=True).click()
                await page.get_by_role("button", name="设备配置", exact=True).click()
                volume = page.get_by_label("初始音量", exact=True)
                await expect(volume).to_have_value("60")
                await expect(page.locator("#count")).to_have_text("1 项未保存")
                await expect(page.get_by_text("设备 test-device", exact=True)).to_be_hidden()
                await volume.fill("85")
                await expect(page.locator("#count")).to_have_text("2 项未保存")
                await page.locator("#save").click()
                await expect(page.locator("#save")).to_be_disabled()
                assert saved[0]["patches"] == [
                    {"path": ["ai_passport", "volume"], "op": "set", "value": 60},
                    {"path": ["ai_passport", "devices", device_id, "volume"], "op": "set", "value": 85},
                ]

                await page.get_by_role("button", name="返回设备").click()
                await page.get_by_role("button", name="配对新设备", exact=True).click()
                await page.get_by_label("设备名称", exact=True).fill("客厅 PP")
                await page.get_by_label("配对码", exact=True).fill("123")
                await page.get_by_role("button", name="确认配对", exact=True).click()
                assert not [x for x in operations if x["action"] == "pair"]
                await page.get_by_label("配对码", exact=True).fill("00123456")
                await page.get_by_role("button", name="确认配对", exact=True).click()
                await expect(page.get_by_text("配对成功，设备会自动连接。")).to_be_visible()
                pair = [x for x in operations if x["action"] == "pair"]
                assert pair == [{"action": "pair", "arguments": {"name": "客厅 PP", "code": "00123456"}}]
                await expect(page.get_by_label("配对码", exact=True)).to_have_value("")
                await page.get_by_role("button", name="设备配置", exact=True).click()
                await page.get_by_role("button", name="撤销配对").click()
                await expect(page.get_by_text("还没有配对设备。", exact=False)).to_be_visible()
                assert [x for x in operations if x["action"] == "revoke"] == [
                    {"action": "revoke", "arguments": {"device_id": device_id}}
                ]
                assert await page.evaluate("localStorage.length + sessionStorage.length") == 0
                assert not errors, errors
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
                assert not errors, errors
            finally:
                await browser.close()
    finally:
        await server.close()
    print(
        "PASS: numeric pairing, device controls and firmware bootstrap form (synthetic; not AP hardware acceptance)"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--screenshot", type=Path)
    args = parser.parse_args()
    asyncio.run(verify(args.screenshot))
