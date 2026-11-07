"""Isolated PC browser checks with a synthetic USB port; never reads or writes real devices."""

import argparse
import asyncio
import json
from pathlib import Path
import time
from aiohttp import web
from aiohttp.test_utils import TestServer
from playwright.async_api import async_playwright, expect


async def verify(site, screenshot=None):
    public = site / "public"
    app = web.Application()
    app.router.add_static("/", public)
    server = TestServer(app)
    await server.start_server()
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page(viewport={"width": 1440, "height": 1100})
            errors, requests = [], []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.on("request", lambda r: requests.append(r.url))
            page.on("dialog", lambda d: asyncio.create_task(d.accept()))
            await page.add_init_script("""
                let receiver;
                window.usbWrites=[];
                Object.defineProperty(navigator, 'serial', {value: {requestPort: async () => ({
                  open: async () => {}, close: async () => {},
                  readable: new ReadableStream({start(c){receiver=c}}),
                  writable: new WritableStream({write(bytes){
                    const line=new TextDecoder().decode(bytes); window.usbWrites.push(line);
                    const response=line.startsWith('pp.status') ? {app:'PA-Terminal',protocol:1}
                      : line.startsWith('pp.reset') ? {reset:true} : {configured:true};
                    receiver.enqueue(new TextEncoder().encode(JSON.stringify(response)+'\\n'));
                  }})
                })}});
            """)
            await page.goto(str(server.make_url("/pp-setup.html")))
            await page.get_by_role("button", name="连接 USB").click()
            await expect(page.locator("#device")).to_contain_text("已连接")
            bundle = {
                "protocol": 1,
                "url": "wss://synthetic.invalid/pa/passport/stream",
                "device_id": "a" * 32,
                "credential": "b" * 43,
                "pairing_token": "c" * 43,
                "expires": time.time() + 600,
                "settings": {"keep_online": True},
            }
            await page.locator("#pairing").set_input_files(
                {
                    "name": "pairing.json",
                    "mimeType": "application/json",
                    "buffer": json.dumps(bundle).encode(),
                }
            )
            await expect(page.locator("#online")).to_be_checked()
            await page.locator("#ssid").fill("合成网络")
            await page.locator("#password").fill("synthetic-only")
            await page.locator("#ca").set_input_files(
                {"name": "ca.crt", "mimeType": "text/plain", "buffer": b"-----BEGIN PRIVATE KEY-----"}
            )
            await page.locator("#save").click()
            await expect(page.locator("#result")).to_contain_text("必须使用公开 CA")
            self_count = await page.evaluate("usbWrites.length")
            assert self_count == 1, "Invalid configuration reached USB"
            await page.locator("#ca").set_input_files(
                {
                    "name": "ca.crt",
                    "mimeType": "text/plain",
                    "buffer": b"-----BEGIN CERTIFICATE-----\nSYNTHETIC\n-----END CERTIFICATE-----",
                }
            )
            if screenshot:
                screenshot.parent.mkdir(parents=True, exist_ok=True)
                await page.screenshot(path=str(screenshot), full_page=True)
            await page.locator("#save").click()
            await expect(page.locator("#result")).to_contain_text("配置已写入")
            writes = await page.evaluate("usbWrites")
            saved = json.loads(writes[1].removeprefix("pp.config "))
            assert saved["settings"]["keep_online"] and saved["ssid"] == "合成网络"
            assert not await page.locator("#password").input_value()
            assert await page.evaluate("localStorage.length + sessionStorage.length") == 0
            assert all(u.startswith(str(server.make_url("/"))) for u in requests)
            await page.get_by_role("button", name="连接 USB").click()
            await expect(page.locator("#device")).to_contain_text("已连接")
            await page.get_by_text("设备维护", exact=True).click()
            await page.locator("#reset").click()
            await expect(page.locator("#result")).to_contain_text("已恢复出厂")
            assert (await page.evaluate("usbWrites"))[-1].strip() == "pp.reset CONFIRM"
            assert not errors, errors
            # Exercise the shipped console device control component using only synthetic administrator replies.
            console = (
                Path(__file__).resolve().parents[1]
                / "src/assistant_runtime/core/configuration/static/console.js"
            )
            source = (
                console.read_text().split("function drawTerminalDevices(", 1)[1].split("\nfunction ", 1)[0]
            )
            await page.evaluate("""() => {
              document.body.replaceChildren();
              window.node=(tag,text,cls)=>{const e=document.createElement(tag); if(text)e.textContent=text;if(cls)e.className=cls;return e};
              window.dirty=()=>false;window.action=fn=>fn();window.operations=[];
              window.api=async(url,body)=>{operations.push(body.action);return body.action==='list'?{devices:[{name:'PP',device_id:'test-device',session_id:'test-session',paired:true,connected:true}]}:{}};
            }""")
            await page.evaluate(
                "(() => { function drawTerminalDevices("
                + source
                + "; window.drawTerminalDevices = drawTerminalDevices; })()"
            )
            await page.evaluate("drawTerminalDevices({id:'ai_passport'},document.body)")
            await page.get_by_role("button", name="刷新设备").click()
            await expect(page.get_by_text("设备 test-device · 会话 test-session")).to_be_visible()
            await page.get_by_role("button", name="撤销配对").click()
            assert "revoke" in await page.evaluate("operations")
            await browser.close()
    finally:
        await server.close()
    print("PASS: USB provisioning, no secret persistence/network upload, console device controls (synthetic)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--site", required=True, type=Path)
    parser.add_argument("--screenshot", type=Path)
    args = parser.parse_args()
    asyncio.run(verify(args.site, args.screenshot))
