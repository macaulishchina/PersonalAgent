"""PC pairing controls against synthetic administrator replies; no device or paid requests."""

import argparse
import asyncio
import ast
import re
from pathlib import Path
from aiohttp import web
from aiohttp.test_utils import TestServer
from playwright.async_api import async_playwright, expect

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

    async def home(_):
        return web.Response(
            text="<!doctype html><html><head><link rel='stylesheet' href='/console.css'></head><body><main id='content'></main></body></html>",
            content_type="text/html",
        )

    app.router.add_get("/", home)
    app.router.add_get("/setup", lambda _: web.Response(text=provisioning_html(), content_type="text/html"))

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
                await page.evaluate("""() => {
                  window.node=(tag,text,cls)=>{const e=document.createElement(tag); if(text)e.textContent=text;if(cls)e.className=cls;return e};
                  window.dirty=()=>false;window.failures=[];window.operations=[];
                  window.key=path=>JSON.stringify(path);window.openEntries=new Set();
                  window.snapshot={fields:[{path:['ai_passport','devices'],value:{}}]};
                  window.currentValue=field=>field.value;window.changes=[];
                  window.changed=(path,op,value)=>changes.push({path,op,value});window.render=()=>{};
                  window.action=async fn=>{try{await fn()}catch(e){failures.push(e.message)}};
                  window.api=async(url,body)=>{operations.push(body);return body.action==='list'?{devices:[{name:'PP',device_id:'test-device',session_id:'test-session',paired:true,connected:true}]}:{paired:true}};
                }""")
                source = (
                    (STATIC / "console.js")
                    .read_text()
                    .split("function drawTerminalDevices(", 1)[1]
                    .split("\nfunction ", 1)[0]
                )
                await page.evaluate(
                    "(() => { function drawTerminalDevices("
                    + source
                    + "; window.drawTerminalDevices=drawTerminalDevices; })()"
                )
                await page.evaluate(
                    "drawTerminalDevices({id:'ai_passport'},document.querySelector('#content'))"
                )
                await page.get_by_role("button", name="刷新设备").click()
                await expect(page.get_by_text("设备 test-device · 会话 test-session")).to_be_visible()
                await page.get_by_role("button", name="设备配置", exact=True).click()
                assert await page.evaluate("changes") == [
                    {"path": ["ai_passport", "devices", "test-device"], "op": "create", "value": {}}
                ]
                await page.get_by_label("设备名称", exact=True).fill("客厅 PP")
                await page.get_by_label("八位配对码", exact=True).fill("123")
                await page.get_by_role("button", name="配对设备", exact=True).click()
                assert not [x for x in await page.evaluate("operations") if x["action"] == "pair"]
                assert "八位" in (await page.evaluate("failures"))[-1]
                await page.get_by_label("八位配对码", exact=True).fill("00123456")
                await page.get_by_role("button", name="配对设备", exact=True).click()
                await expect(page.get_by_text("配对成功，设备会自动连接。")).to_be_visible()
                pair = [x for x in await page.evaluate("operations") if x["action"] == "pair"]
                assert pair == [{"action": "pair", "arguments": {"name": "客厅 PP", "code": "00123456"}}]
                assert not await page.get_by_label("八位配对码", exact=True).input_value()
                await page.get_by_role("button", name="撤销配对").click()
                assert any(x["action"] == "revoke" for x in await page.evaluate("operations"))
                assert await page.evaluate("localStorage.length + sessionStorage.length") == 0
                assert not errors, errors
                if screenshot:
                    screenshot.parent.mkdir(parents=True, exist_ok=True)
                    await page.screenshot(path=str(screenshot), full_page=True)
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
