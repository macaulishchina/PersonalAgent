"""Isolated desktop console acceptance: synthetic files/local model replies, no paid or device calls.

Requires Playwright, its Chromium browser and the installed sample plugin. Use a
separate validation environment, never install test dependencies into a running service.
"""

import argparse
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import time
import asyncio
import json
import os
import socket
import tempfile
import tomllib
import subprocess
import sys
from pathlib import Path
from playwright.async_api import async_playwright, expect


@contextmanager
def directory_fixture():
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            assert self.path.endswith("/responses")
            assert payload["max_output_tokens"] <= 1024
            assert "connectivity test" in json.dumps(payload)
            assert not payload.get("tools")
            item = {"id": "probe", "type": "message", "role": "assistant", "content": []}
            events = [
                {"type": "response.created", "response": {"id": "probe", "status": "in_progress"}},
                {"type": "response.output_item.added", "output_index": 0, "item": item},
                {
                    "type": "response.content_part.added",
                    "item_id": "probe",
                    "output_index": 0,
                    "content_index": 0,
                    "part": {"type": "output_text", "text": ""},
                },
                {
                    "type": "response.output_text.delta",
                    "item_id": "probe",
                    "output_index": 0,
                    "content_index": 0,
                    "delta": "OK",
                },
                {
                    "type": "response.output_item.done",
                    "output_index": 0,
                    "item": {
                        **item,
                        "status": "completed",
                        "content": [{"type": "output_text", "text": "OK"}],
                    },
                },
                {
                    "type": "response.completed",
                    "response": {
                        "id": "probe",
                        "status": "completed",
                        "usage": {"input_tokens": 20, "output_tokens": 1},
                    },
                },
            ]
            body = "".join("data: " + json.dumps(event) + "\n\n" for event in events).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path.startswith("/slow/"):
                time.sleep(0.3)
            denied = self.path.startswith("/denied/") or (
                self.path.startswith("/draft/")
                and self.headers.get("Authorization") != "Bearer synthetic-draft-key"
            )
            data = (
                {"error": "synthetic-private-key"}
                if denied
                else {
                    "data": [
                        {
                            "id": "catalog-fast",
                            "context_window": 64000,
                            "reasoning_efforts": ["low", "high"],
                            "output_limit": 4096,
                            "images": False,
                        },
                        {"id": "catalog-quality"},
                        {"id": "long-" + "model" * 35},
                    ]
                    + [{"id": f"catalog-option-{i:02}"} for i in range(25)]
                }
            )
            body = json.dumps(data).encode()
            self.send_response(401 if denied else 200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def selector(path):
    return "[data-field-path=" + json.dumps(json.dumps(path, separators=(",", ":"))) + "]"


def arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--python", default=sys.executable, help="Application runtime Python; defaults to this interpreter"
    )
    parser.add_argument(
        "--installed", action="store_true", help="Test an installed wheel rather than repository source"
    )
    parser.add_argument("--browser", help="Optional Chromium executable")
    parser.add_argument("--artifacts", type=Path, help="Optional directory for synthetic screenshots")
    return parser.parse_args()


async def main():
    args = arguments()
    runtime_python = str(Path(args.python).expanduser().absolute())
    repository = Path(__file__).resolve().parents[1]
    environment = {k: v for k, v in os.environ.items() if not k.startswith("PERSONAL_AGENT_")}
    environment["PYTHONPATH"] = "" if args.installed else str(repository / "src")
    probe = subprocess.run(
        [
            runtime_python,
            "-c",
            "from importlib.metadata import distribution; distribution('personal-agent-sample')",
        ],
        env=environment,
        capture_output=True,
    )
    if probe.returncode:
        raise SystemExit("Install the sample plugin into the isolated application runtime before this check.")
    if args.artifacts:
        args.artifacts = args.artifacts.expanduser().absolute()
        args.artifacts.mkdir(parents=True, exist_ok=True, mode=0o700)

    async def capture(page, name, **options):
        if args.artifacts:
            await page.screenshot(path=str(args.artifacts / name), **options)

    with (
        tempfile.TemporaryDirectory(prefix="pa-desktop-console-") as folder,
        directory_fixture() as directory,
    ):
        root = Path(folder) / "instance"
        env = environment
        setup = await asyncio.create_subprocess_exec(
            runtime_python,
            "-m",
            "assistant_runtime.app.cli",
            "init",
            str(root),
            "--preset",
            "slim",
            env=env,
            stdout=-1,
            stderr=-1,
        )
        out, err = await setup.communicate()
        assert setup.returncode == 0, err.decode()
        entry = root / "config/config.toml"
        import tomlkit

        document = tomlkit.parse(entry.read_text())
        if sys.platform == "linux":
            document["plugins"].append("sample")
        else:
            document["plugin_settings"] = {"sample": {}}
        platform = "windows" if sys.platform == "win32" else "linux"
        document["plugin_backends"] = {"pipewire": {platform: "removed_for_acceptance"}}
        entry.write_text(tomlkit.dumps(document))
        models = root / "config/models.toml"
        models.write_text(
            models.read_text()
            + """
[model_api.providers.openai]
preset = "openai"
enabled = false
[model_api.providers.deepseek]
preset = "deepseek"
enabled = false
[model_api.providers.gemini]
preset = "gemini"
enabled = false
[model_api.entries.primary]
provider = "openai"
model = "gpt-6.1-sol"
reasoning_efforts = ["medium", "high"]
default_reasoning_effort = "medium"
images = true
structured = true
[model_api.entries.fast]
provider = "deepseek"
model = "deepseek-flash"
reasoning_efforts = ["none", "high"]
default_reasoning_effort = "none"
"""
        )
        with models.open("a", encoding="utf-8") as model_file:
            for name, path in (("directory", "v1"), ("denied", "denied"), ("slow", "slow")):
                model_file.write(
                    f'\n[model_api.providers.{name}]\nprotocol="openai_responses"\nbase_url="{directory}/{path}"\nkey_env="PA_DIRECTORY_KEY"\nenabled=false\n'
                )
        (root / ".env").write_text(
            "DEEPSEEK_API_KEY=synthetic-secret-before\nPA_DIRECTORY_KEY=synthetic-private-key\n"
        )
        management = root / "management.toml"
        p = port()
        management.write_text(management.read_text().replace("0.0.0.0", "127.0.0.1").replace("8047", str(p)))
        log = (root / "manager.log").open("wb")
        process = await asyncio.create_subprocess_exec(
            runtime_python,
            "-m",
            "assistant_runtime.app.cli",
            "configuration",
            "serve",
            "--management",
            str(management),
            env=env,
            stdout=log,
            stderr=log,
        )
        origin = f"http://127.0.0.1:{p}"
        errors = []
        try:
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(executable_path=args.browser, headless=True)
                page = await browser.new_page(viewport={"width": 1440, "height": 1000})
                page.on("pageerror", lambda e: errors.append(str(e)))
                page.on("dialog", lambda dialog: dialog.accept())
                async with asyncio.timeout(25):
                    while True:
                        assert process.returncode is None
                        try:
                            await page.goto(origin)
                            break
                        except Exception:
                            await asyncio.sleep(0.1)
                await capture(page, "configuration-desktop-login.png")
                await page.get_by_label("管理员密码", exact=True).fill(
                    (root / "state/configuration/admin-token").read_text().strip()
                )
                await page.get_by_role("button", name="登录", exact=True).click()
                await expect(page.locator("#editor")).to_be_visible()
                await expect(page.get_by_role("button", name="模型与账号", exact=True)).to_be_visible()
                async with asyncio.timeout(25):
                    while (initial := await (await page.request.get(origin + "/api/status")).json())[
                        "state"
                    ] != "running":
                        assert initial["state"] != "failed", initial.get("error")
                        await asyncio.sleep(0.1)
                pid = initial["pid"]
                await expect(page.locator("#health")).to_have_text("业务运行中")
                assert not errors, errors

                async def plugin_failure(route):
                    response = await route.fetch()
                    data = await response.json()
                    data["state"] = "degraded"
                    data["plugin_issues"] = [
                        {"id": "fixture-plugin", "state": "failed", "error": "缺少本地资源，请完成插件部署。"}
                    ]
                    await route.fulfill(response=response, json=data)

                await page.route("**/api/status", plugin_failure)
                await expect(page.locator("#plugin-health")).to_contain_text("fixture-plugin", timeout=12000)
                await expect(page.locator("#health")).to_have_text("部分功能不可用")
                await page.unroute("**/api/status", plugin_failure)
                await expect(page.locator("#plugin-health")).to_be_hidden(timeout=12000)
                await expect(page.locator("#health")).to_have_text("业务运行中")
                await capture(page, "configuration-desktop-access.png")
                await page.get_by_role("button", name="功能与插件", exact=True).click()
                release_catalog = await (await page.request.get(origin + "/api/catalog")).json()
                await expect(
                    page.get_by_role(
                        "heading", name="内核 " + release_catalog["core_release"]["version"], exact=True
                    )
                ).to_be_visible()
                await expect(page.locator("#state-version")).to_contain_text("配置修订")
                sample = page.locator(".plugin-option").filter(has_text="示例笔记").locator("input")
                if sys.platform == "linux":
                    await sample.uncheck()
                    await sample.check()
                    await page.get_by_role("button", name="放弃修改", exact=True).click()
                    await expect(page.locator("#discard")).to_be_disabled()
                else:
                    await expect(sample).to_be_disabled()
                backend = page.get_by_label("本机音频默认实现", exact=True)
                await expect(backend).to_have_value("removed_for_acceptance")
                await expect(page.get_by_label("本机摄像头默认实现", exact=True)).to_have_count(0)
                await backend.select_option("")
                async with page.expect_response(
                    lambda r: r.url.endswith("/api/configuration") and r.request.method == "POST"
                ) as save_response:
                    await page.get_by_role("button", name="保存并生效", exact=True).click()
                saved_response = await save_response.value
                assert saved_response.status == 200, await saved_response.json()
                await expect(page.locator("#documents")).not_to_have_attribute("inert", "")
                await expect(backend).to_have_count(0)
                assert (
                    not tomllib.loads(entry.read_text())
                    .get("plugin_backends", {})
                    .get("pipewire", {})
                    .get(platform)
                )
                assert (await (await page.request.get(origin + "/api/status")).json())["pid"] == pid
                audio = page.locator('.plugin-option input[type="checkbox"][value="pipewire"]')
                await audio.check()
                await expect(backend).to_have_count(0)
                await page.get_by_role("button", name="放弃修改", exact=True).click()
                await expect(page.locator("#discard")).to_be_disabled()
                await capture(page, "configuration-desktop-plugins.png")
                await (
                    page.locator(".plugin-option")
                    .filter(has_text="示例笔记")
                    .get_by_role("button", name="配置", exact=True)
                    .click()
                )
                maximum = page.get_by_label("笔记长度上限", exact=True)
                await maximum.fill("0")
                await expect(page.locator("#save")).to_be_disabled()
                await expect(page.locator(".field-error:visible")).to_contain_text("1")
                await capture(page, "configuration-desktop-validation.png")
                await page.get_by_role("button", name="实例管理", exact=True).click()
                await expect(page.locator("#leave-save")).to_be_disabled()
                await page.get_by_role("button", name="继续编辑", exact=True).click()
                await expect(maximum).to_have_value("0")
                await maximum.fill("1200")
                await page.get_by_label("报告风格", exact=True).select_option("detailed")
                await expect(page.get_by_label("报告风格", exact=True)).to_have_value("detailed")
                await page.get_by_label("标题", exact=True).fill("执行报告")
                async with page.expect_response(
                    lambda r: r.url.endswith("/api/configuration") and r.request.method == "POST"
                ) as save_response:
                    await page.get_by_role("button", name="保存并生效", exact=True).click()
                saved_response = await save_response.value
                assert saved_response.status == 200, await saved_response.json()
                await expect(page.locator("#notice")).to_have_text("保存成功，设置已生效。")
                config = tomllib.loads(entry.read_text())
                assert config["plugin_settings"]["sample"]["report"]["style"] == "detailed"
                assert config["plugin_settings"]["sample"]["max_length"] == 1200
                assert (await (await page.request.get(origin + "/api/status")).json())["pid"] == pid
                await page.get_by_role("button", name="模型与账号", exact=True).click()
                await page.get_by_role("tab", name="高级", exact=True).click()
                await expect(page.get_by_text("部署维护 · 本机模型网关", exact=True)).to_have_count(0)
                await expect(page.get_by_label("本机网关地址", exact=True)).to_have_count(0)
                await capture(page, "configuration-model-policy.png")
                await page.get_by_role("tab", name="连接", exact=True).click()
                await page.get_by_role("tab", name="连接", exact=True).focus()
                await page.keyboard.press("ArrowRight")
                await expect(page.get_by_role("tab", name="模型库", exact=True)).to_be_focused()
                await page.keyboard.press("ArrowLeft")
                await expect(page.get_by_role("tab", name="连接", exact=True)).to_be_focused()
                preset_card = page.locator('.entity-card[data-entity="openai"]')
                await preset_card.get_by_role("button", name="编辑", exact=True).click()
                await expect(page.locator("#entry-dialog #connection-key")).to_be_visible()
                await expect(
                    page.locator("#entry-dialog").get_by_label("已有凭据或环境变量名称", exact=True)
                ).to_be_hidden()
                await expect(
                    page.locator("#entry-dialog").get_by_label("接口地址", exact=True)
                ).to_have_attribute("readonly", "")
                await expect(
                    page.locator("#entry-dialog").get_by_label("接口协议", exact=True)
                ).to_be_hidden()
                await page.locator("#entry-dialog").get_by_role("button", name="取消编辑", exact=True).click()
                directory_card = page.locator('.entity-card[data-entity="directory"]')
                await directory_card.get_by_role("button", name="测试连接", exact=True).click()
                await expect(directory_card.locator(".provider-status")).to_contain_text("已连通")
                await capture(page, "configuration-provider-test.png")
                # Reproduce old generic editor drafts with empty mutually exclusive fields.
                await page.evaluate("""() => changed(["model_api","providers","directory"], "set", {
                    ...valueAt(["model_api","providers"], {}).directory, preset: "", account: ""
                })""")
                await directory_card.get_by_role("button", name="编辑", exact=True).click()
                drawer = page.locator("#entry-dialog")
                await expect(drawer.get_by_role("button", name="测试连接", exact=True)).to_be_visible()
                await capture(page, "configuration-connection-editor.png")
                saved_before_probe = entry.read_bytes()
                credential_before_probe = (root / ".env").read_bytes()
                await drawer.get_by_label("接口地址", exact=True).fill(directory + "/draft")
                await drawer.locator("#connection-key").fill("synthetic-draft-key")
                advanced = drawer.locator("details.connection-protocol-options")
                await advanced.locator(":scope > summary").click()
                await drawer.get_by_label("会话请求头", exact=True).fill("33")
                await drawer.get_by_role("button", name="测试连接", exact=True).click()
                await expect(drawer.locator(".provider-status")).to_contain_text("会话请求头")
                await expect(drawer.get_by_label("会话请求头", exact=True)).to_be_focused()
                await drawer.get_by_label("会话请求头", exact=True).fill("")
                await drawer.get_by_role("button", name="测试连接", exact=True).click()
                await expect(drawer.locator(".provider-status")).to_contain_text("已连通")
                await expect(drawer.get_by_label("已有凭据或环境变量名称", exact=True)).to_be_hidden()
                body = await page.evaluate("providerDraft('directory')")
                assert not {"preset", "account", "auth_scheme", "session_header"}.intersection(
                    body["provider"]
                )
                assert body["key"] == "synthetic-draft-key"
                assert body["provider"]["key_env"] != "PA_DIRECTORY_KEY"
                assert (
                    await page.evaluate("valueAt(['model_api','providers'], {}).denied.key_env")
                    == "PA_DIRECTORY_KEY"
                )
                await drawer.get_by_label("接口类型", exact=True).select_option("deepseek")
                await expect(drawer.get_by_label("接口地址", exact=True)).to_have_attribute("readonly", "")
                await expect(drawer.locator("#connection-key")).to_have_value("")
                body = await page.evaluate("providerDraft('directory')")
                assert set(body["provider"]) == {"preset", "enabled", "key_env"}
                assert "key" not in body
                await drawer.get_by_label("接口类型", exact=True).select_option("custom")
                await expect(drawer.get_by_label("接口地址", exact=True)).to_have_value(directory + "/draft")
                await drawer.locator("#connection-key").fill("synthetic-draft-key")
                await drawer.locator(".connection-auth-options > summary").click()
                await drawer.get_by_label("密钥来源", exact=True).select_option("reference")
                await expect(drawer.locator("#connection-key")).to_be_hidden()
                await drawer.get_by_label("已有凭据或环境变量名称", exact=True).fill("PA_DIRECTORY_KEY")
                body = await page.evaluate("providerDraft('directory')")
                assert body["provider"]["key_env"] == "PA_DIRECTORY_KEY"
                assert "key" not in body
                await drawer.get_by_label("密钥来源", exact=True).select_option("managed")
                await drawer.locator(".connection-auth-options > summary").click()
                assert entry.read_bytes() == saved_before_probe
                assert (root / ".env").read_bytes() == credential_before_probe
                await capture(page, "configuration-draft-test.png")
                await drawer.get_by_role("button", name="取消编辑", exact=True).click()
                await page.get_by_role("button", name="放弃修改", exact=True).click()
                await expect(page.locator("#discard")).to_be_disabled()
                denied = page.locator('.entity-card[data-entity="denied"]')
                await denied.get_by_role("button", name="测试连接", exact=True).click()
                await expect(denied.locator(".provider-status")).to_contain_text("认证未通过")
                assert "synthetic-private-key" not in await denied.inner_text()
                await page.get_by_role("tab", name="模型库", exact=True).click()
                await page.get_by_role("button", name="添加模型", exact=True).click()
                setup = page.locator("#setup-dialog")
                await setup.get_by_label("使用连接", exact=True).select_option("directory")
                await setup.get_by_role("button", name="查询模型", exact=True).click()
                await expect(
                    setup.get_by_role("button", name="选择 catalog-fast", exact=True)
                ).to_be_visible()
                for width in (1024, 1440):
                    await page.set_viewport_size({"width": width, "height": 1000})
                    assert await setup.evaluate("d => d.scrollWidth <= d.clientWidth + 1")
                    assert await setup.locator(".dialog-actions").evaluate(
                        "e => e.getBoundingClientRect().bottom <= innerHeight"
                    )
                await capture(page, "configuration-model-query.png")
                await setup.get_by_role("searchbox", name="搜索查询到的模型", exact=True).fill("catalog-fast")
                await setup.get_by_role("button", name="选择 catalog-fast", exact=True).press("Enter")
                await expect(setup.get_by_label("供应商模型 ID", exact=True)).to_have_value("catalog-fast")
                await expect(setup.get_by_label("在 PA 中的名称", exact=True)).to_have_value("catalog-fast")
                await expect(setup.locator(".model-results")).to_be_hidden()
                await setup.get_by_role("button", name="展开列表", exact=True).click()
                await expect(
                    setup.get_by_role("button", name="已选中 catalog-fast", exact=True)
                ).to_be_visible()
                await setup.get_by_label("使用连接", exact=True).select_option("slow")
                async with page.expect_response(lambda r: r.url.endswith("/api/provider-query")):
                    await setup.get_by_role("button", name="查询模型", exact=True).click()
                    await setup.get_by_label("使用连接", exact=True).select_option("denied")
                await expect(setup.locator(".model-results")).to_be_hidden()
                await expect(setup.get_by_label("供应商模型 ID", exact=True)).to_have_value("")
                await setup.get_by_role("button", name="查询模型", exact=True).click()
                await expect(setup.locator(".model-picker > [role=status]")).to_contain_text("认证未通过")
                await setup.get_by_label("供应商模型 ID", exact=True).fill("manual-still-allowed")
                await setup.get_by_role("button", name="取消", exact=True).click()
                await page.get_by_role("tab", name="连接", exact=True).click()
                await page.get_by_role("button", name="添加连接", exact=True).click()
                setup = page.locator("#setup-dialog")
                await capture(page, "configuration-desktop-connection-wizard.png")
                await setup.get_by_role("radio", name="通过 API 连接", exact=True).check()
                await setup.get_by_label("连接名称", exact=True).fill("LeiHuo-OpenAI")
                await setup.get_by_label("接口类型", exact=True).select_option("custom")
                await setup.get_by_label("接口地址", exact=True).fill(directory + "/draft")
                await setup.locator("#setup-api-key").fill("synthetic-draft-key")
                await setup.get_by_role("button", name="添加连接", exact=True).click()
                await expect(page.locator(".entity-card[data-entity=LeiHuo-OpenAI]")).to_be_visible()
                # A new unsaved connection uses its own draft endpoint/key without saving.
                await (
                    page.locator('.entity-card[data-entity="LeiHuo-OpenAI"]')
                    .get_by_role("button", name="测试连接", exact=True)
                    .click()
                )
                await expect(
                    page.locator('.entity-card[data-entity="LeiHuo-OpenAI"] .provider-status')
                ).to_contain_text("已连通")
                assert entry.read_bytes() == saved_before_probe
                assert (root / ".env").read_bytes() == credential_before_probe
                await page.get_by_role("tab", name="模型库", exact=True).click()
                await page.get_by_role("button", name="添加模型", exact=True).click()
                await setup.get_by_label("使用连接", exact=True).select_option("LeiHuo-OpenAI")
                await setup.get_by_role("button", name="查询模型", exact=True).click()
                await expect(
                    setup.get_by_role("button", name="选择 catalog-fast", exact=True)
                ).to_be_visible()
                await setup.get_by_role("button", name="取消", exact=True).click()
                await page.get_by_role("tab", name="连接", exact=True).click()
                # Native setup stages account, connection and private credential atomically.
                await page.get_by_role("button", name="添加连接", exact=True).click()
                await setup.get_by_label("连接名称", exact=True).fill("native_test")
                await setup.get_by_label("供应商", exact=True).fill("openai")
                await setup.locator("#setup-credential").fill("synthetic-native-key")
                await setup.get_by_role("button", name="添加连接", exact=True).click()
                await expect(page.locator(".entity-card[data-entity=native_test]")).to_be_visible()
                await expect(page.locator("#documents")).not_to_contain_text("synthetic-native-key")
                # A wizard cancellation creates neither an account nor a credential.
                await page.get_by_role("button", name="添加连接", exact=True).click()
                await setup.get_by_label("连接名称", exact=True).fill("cancelled_connection")
                await setup.locator("#setup-credential").fill("discarded-secret")
                await setup.get_by_role("button", name="取消", exact=True).click()
                await expect(page.locator(".entity-card[data-entity=cancelled_connection]")).to_have_count(0)
                # Creating a connection is still a draft; switching model tabs must retain it.
                await page.get_by_role("button", name="添加连接", exact=True).click()
                await setup.get_by_role("radio", name="通过 API 连接", exact=True).check()
                await setup.get_by_label("连接名称", exact=True).fill("discard_me")
                await setup.get_by_label("接口类型", exact=True).select_option("deepseek")
                await setup.get_by_role("button", name="添加连接", exact=True).click()
                await (
                    page.locator(".entity-card[data-entity=discard_me]")
                    .get_by_role("button", name="移除", exact=True)
                    .click()
                )
                await expect(page.locator(".entity-card[data-entity=discard_me]")).to_have_count(0)
                await capture(page, "configuration-desktop-connections.png")
                await page.get_by_role("tab", name="模型库", exact=True).click()
                search = page.get_by_role("searchbox", name="搜索名称、模型 ID 或连接", exact=True)
                await search.fill("deepseek")
                await expect(page.locator(".entity-card[data-entity=primary]")).not_to_be_visible()
                await expect(page.locator(".entity-card[data-entity=fast]")).to_be_visible()
                await search.fill("does-not-exist")
                await expect(page.locator(".search-empty")).to_be_visible()
                await search.fill("")
                await capture(page, "configuration-desktop-library.png")
                # Cancelling a newly created model restores the pre-wizard draft.
                await page.get_by_role("button", name="添加模型", exact=True).click()
                await setup.get_by_label("供应商模型 ID", exact=True).fill("synthetic-model")
                await setup.get_by_label("在 PA 中的名称", exact=True).fill("cancelled_model")
                await setup.get_by_role("button", name="添加并完善设置", exact=True).click()
                await page.locator("#entry-dialog").get_by_role("button", name="取消编辑", exact=True).click()
                await expect(page.locator(".entity-card[data-entity=cancelled_model]")).to_have_count(0)

                # Public references suggest across suppliers, but only explicit adoption stages facts.
                await page.get_by_role("button", name="添加模型", exact=True).click()
                await setup.get_by_label("供应商模型 ID", exact=True).fill("deepseek-v4.1-flash")
                await setup.get_by_label("在 PA 中的名称", exact=True).fill("reference_preview")
                await setup.get_by_role("button", name="添加并完善设置", exact=True).click()
                reference_editor = page.locator("#entry-dialog")
                picker = reference_editor.locator(".capability-picker")
                candidate = picker.get_by_role("button", name="匹配的参考模型", exact=True)
                await expect(candidate).to_be_enabled()
                await expect(candidate).to_have_attribute("aria-expanded", "false")
                await expect(picker.get_by_role("button", name="填入参数", exact=True)).to_be_disabled()
                original_connection = await reference_editor.get_by_label(
                    "使用连接", exact=True
                ).input_value()
                search_reference = picker.get_by_label("搜索模型或服务商", exact=True)
                await search_reference.fill("no-such-reference-xyz")
                await expect(picker).to_contain_text("暂无匹配资料")
                await expect(candidate).to_be_disabled()
                await search_reference.fill("deepseek-v4.1-flash")
                await expect(candidate).to_be_enabled()
                await candidate.click()
                filter_input = picker.get_by_role(
                    "searchbox", name="过滤服务商或模型（可输入原厂）", exact=True
                )
                await expect(filter_input).to_be_focused()
                dropdown_bounds = await picker.locator(".searchable-select-panel").bounding_box()
                editor_bounds = await reference_editor.locator("#entry-body").bounding_box()
                assert dropdown_bounds["y"] >= editor_bounds["y"]
                assert (
                    dropdown_bounds["y"] + dropdown_bounds["height"]
                    <= editor_bounds["y"] + editor_bounds["height"] + 1
                )
                options = picker.locator(".searchable-select-option")
                assert await options.count() > 50  # includes every page from the former UI
                await filter_input.fill("官方")
                assert await picker.locator(".searchable-select-option:visible").count() > 0
                await expect(options.first).to_contain_text("原厂目录")
                await filter_input.press("Escape")
                await search_reference.fill("deepseek")
                await expect(candidate).to_be_enabled()
                await candidate.click()
                await expect(options.first).to_contain_text("原厂目录")
                await filter_input.fill("原厂")
                original_rows = picker.locator(".searchable-select-option:visible")
                assert await original_rows.count() > 0
                for index in range(await original_rows.count()):
                    assert (await original_rows.nth(index).get_attribute("data-value")).startswith(
                        "deepseek/"
                    )
                await filter_input.fill("官方")
                assert await original_rows.count() > 0
                await filter_input.press("Escape")
                await search_reference.fill("deepseek-v4.1-flash")
                await expect(candidate).to_be_enabled()
                await candidate.click()
                selected_reference = await options.last.get_attribute("data-value")
                selected_label = await options.last.inner_text()
                await filter_input.press("Escape")
                await expect(candidate).to_have_attribute("aria-expanded", "false")
                await expect(reference_editor).to_be_visible()
                await candidate.click()
                await filter_input.fill("no-such-supplier-xyz")
                await expect(picker.locator(".searchable-select-panel .search-empty")).to_be_visible()
                await filter_input.fill(selected_label)
                await expect(picker.locator(".searchable-select-option:visible")).to_have_count(1)
                await capture(page, "configuration-reference-dropdown.png")
                await filter_input.press("ArrowDown")
                await page.keyboard.press("Enter")
                await expect(candidate).to_have_attribute("aria-expanded", "false")
                await candidate.click()
                await filter_input.fill("")
                assert (
                    await picker.locator(".searchable-select-option:visible").count() == await options.count()
                )
                await filter_input.press("Escape")
                await expect(picker.locator(".capability-preview")).to_contain_text(selected_reference)
                await expect(reference_editor.locator(".capability-summary")).to_contain_text("可手动填写")
                await expect(reference_editor.get_by_text("capabilities", exact=True)).to_have_count(0)
                await picker.locator(".capability-preview").scroll_into_view_if_needed()
                await capture(page, "configuration-model-reference.png")
                await reference_editor.get_by_label("上下文窗口", exact=True).fill("512")
                await reference_editor.get_by_label("支持图片", exact=True).uncheck()
                await candidate.click()
                await filter_input.fill("deepseek-flash · deepseek · 原厂目录")
                original_choice = picker.locator(".searchable-select-option:visible").first
                selected_reference = await original_choice.get_attribute("data-value")
                await original_choice.click()
                await picker.get_by_role("button", name="填入参数", exact=True).click()
                await expect(reference_editor.locator(".capability-summary")).to_contain_text(
                    "最近采用：models.dev"
                )
                await expect(reference_editor.get_by_label("供应商模型 ID", exact=True)).to_have_value(
                    "deepseek-v4.1-flash"
                )
                await expect(reference_editor.get_by_label("使用连接", exact=True)).to_have_value(
                    original_connection
                )
                await expect(reference_editor.get_by_label("默认思考模式", exact=True)).to_have_value("auto")
                for effort in ("low", "high", "max"):
                    await expect(
                        reference_editor.get_by_role("group", name="可用思考档位", exact=True).locator(
                            f'input[value="{effort}"]'
                        )
                    ).to_be_checked()
                await expect(
                    reference_editor.get_by_role("group", name="可用思考档位", exact=True).locator(
                        'input[value="medium"]'
                    )
                ).not_to_be_checked()
                from assistant_runtime.core.models.capabilities import public_catalog

                selected_facts = public_catalog()["models"][selected_reference]
                await expect(reference_editor.get_by_label("上下文窗口", exact=True)).to_have_value(
                    str(selected_facts["context_window"])
                )
                await expect(reference_editor.get_by_label("模型最大输出", exact=True)).to_have_value(
                    str(selected_facts["output_limit"])
                )
                self_images = reference_editor.get_by_label("支持图片", exact=True)
                assert await self_images.is_checked() == selected_facts["images"]
                await reference_editor.get_by_label("上下文窗口", exact=True).fill("87654")
                await expect(reference_editor.get_by_label("上下文窗口", exact=True)).to_have_value("87654")
                await reference_editor.get_by_role("button", name="取消编辑", exact=True).click()
                await expect(page.locator(".entity-card[data-entity=reference_preview]")).to_have_count(0)

                # Gemini imports explicit public levels into checkboxes and default choices.
                await page.get_by_role("button", name="添加模型", exact=True).click()
                await setup.get_by_label("使用连接", exact=True).select_option("gemini")
                await setup.get_by_label("供应商模型 ID", exact=True).fill("gemini-3.1-pro-preview")
                await setup.get_by_label("在 PA 中的名称", exact=True).fill("gemini_preview")
                await setup.get_by_role("button", name="添加并完善设置", exact=True).click()
                await picker.locator("summary").click()
                await candidate.click()
                await picker.locator(
                    '.searchable-select-option[data-value="google/gemini-3.1-pro-preview"]'
                ).click()
                await picker.get_by_role("button", name="填入参数", exact=True).click()
                await expect(reference_editor.locator(".capability-summary")).to_contain_text(
                    "最近采用：models.dev"
                )
                levels = reference_editor.get_by_role("group", name="可用思考档位", exact=True)
                for effort in ("low", "medium", "high"):
                    await expect(levels.locator(f'input[value="{effort}"]')).to_be_checked()
                for effort in ("none", "minimal", "xhigh", "max", "ultra"):
                    await expect(levels.locator(f'input[value="{effort}"]')).not_to_be_checked()
                default_effort = reference_editor.get_by_label("默认思考模式", exact=True)
                await expect(default_effort).to_have_value("auto")
                await default_effort.select_option("medium")
                await expect(default_effort).to_have_value("medium")
                await reference_editor.get_by_role("button", name="取消编辑", exact=True).click()
                await expect(page.locator(".entity-card[data-entity=gemini_preview]")).to_have_count(0)

                # Selected metadata remains a draft, survives save, and grants no execution acceptance.
                await page.get_by_role("button", name="添加模型", exact=True).click()
                await setup.get_by_label("使用连接", exact=True).select_option("directory")
                await setup.get_by_role("button", name="查询模型", exact=True).click()
                await setup.get_by_role("button", name="选择 catalog-fast", exact=True).click()
                await setup.get_by_label("在 PA 中的名称", exact=True).fill("bad name")
                assert not await setup.get_by_label("在 PA 中的名称", exact=True).evaluate(
                    "input => input.checkValidity()"
                )
                await setup.get_by_label("在 PA 中的名称", exact=True).fill("automatic.model")
                await setup.get_by_role("button", name="添加并完善设置", exact=True).click()
                auto_editor = page.locator("#entry-dialog")
                await expect(auto_editor.get_by_label("默认思考模式", exact=True)).to_have_value("auto")
                await expect(auto_editor.get_by_label("上下文窗口", exact=True)).to_have_value("64000")
                await expect(auto_editor.get_by_label("模型最大输出", exact=True)).to_have_value("4096")
                assert (
                    await auto_editor.get_by_label("默认思考模式", exact=True).locator("option").count() == 3
                )
                await auto_editor.get_by_label("默认思考模式", exact=True).select_option("high")
                await (
                    auto_editor.get_by_role("group", name="可用思考档位", exact=True)
                    .locator('input[value="high"]')
                    .uncheck()
                )
                await expect(auto_editor.get_by_label("默认思考模式", exact=True)).to_have_value("auto")
                await auto_editor.get_by_label("上下文窗口", exact=True).fill("64001")
                await auto_editor.get_by_label("模型最大输出", exact=True).fill("2048")
                await auto_editor.get_by_label("支持图片", exact=True).uncheck()
                await capture(page, "configuration-automatic-model.png")
                await auto_editor.get_by_role("button", name="保留修改", exact=True).click()

                # One click tests an unsaved model through the real operator/usage/Pi path.
                draft_card = page.locator('.entity-card[data-entity="automatic.model"]')
                config_before_probe = models.read_bytes()
                await draft_card.get_by_role("button", name="测试模型", exact=True).click()
                await expect(draft_card.locator(".provider-status")).to_contain_text(
                    "文本推理可用", timeout=30000
                )
                await expect(draft_card.locator(".provider-status")).to_contain_text("回复：OK")
                await expect(draft_card).not_to_contain_text("执行待验收")
                await expect(draft_card).not_to_contain_text("执行已验收")
                assert models.read_bytes() == config_before_probe

                await (
                    page.locator(".entity-card[data-entity=primary]")
                    .get_by_role("button", name="编辑", exact=True)
                    .click()
                )
                primary = page.locator("#entry-dialog")
                await expect(primary.get_by_label("默认思考模式", exact=True)).to_be_visible()
                await expect(primary.locator(".capability-summary")).to_contain_text("模型参数")
                await expect(primary.get_by_label("执行能力已经验收", exact=True)).to_have_count(0)
                await primary.get_by_label("使用连接", exact=True).select_option("LeiHuo-OpenAI")
                await expect(primary.get_by_label("使用连接", exact=True)).to_have_value("LeiHuo-OpenAI")
                efforts = primary.get_by_role("group", name="可用思考档位", exact=True)
                await efforts.locator("input[value=none]").check()
                await efforts.locator("input[value=high]").uncheck()
                await primary.get_by_text("高级设置", exact=True).click()
                await primary.get_by_text("高级设置", exact=True).click()
                await primary.evaluate("dialog => dialog.scrollTop = 0")
                await capture(page, "configuration-desktop-models.png")
                await primary.get_by_role("button", name="保留修改", exact=True).click()
                # Cancel an editor without losing a different model's pending edits.
                await (
                    page.locator(".entity-card[data-entity=fast]")
                    .get_by_role("button", name="编辑", exact=True)
                    .click()
                )
                await primary.get_by_label("供应商模型 ID", exact=True).fill("discarded-draft")
                await page.keyboard.press("Escape")
                await expect(primary).not_to_be_visible()
                await expect(page.locator(".entity-card[data-entity=fast]")).not_to_contain_text(
                    "discarded-draft"
                )
                await page.get_by_role("tab", name="用途", exact=True).click()
                await expect(page.get_by_label("主对话模型", exact=True)).to_be_visible()
                await capture(page, "configuration-desktop-usage.png")
                async with page.expect_response(
                    lambda r: r.url.endswith("/api/configuration") and r.request.method == "POST"
                ) as save_response:
                    await page.get_by_role("button", name="保存并生效", exact=True).click()
                saved_response = await save_response.value
                assert saved_response.status == 200, await saved_response.json()
                await expect(page.locator("#notice")).to_have_text("保存成功，设置已生效。")
                saved = tomllib.loads(models.read_text())["model_api"]
                assert saved["providers"]["native_test"]["account"] == "native_test"
                native_reference = saved["accounts"]["native_test"]["key_env"]
                assert native_reference.startswith("PA_CONNECTION_")
                assert "cancelled_connection" not in saved["providers"]
                assert "cancelled_model" not in saved["entries"]
                automatic = saved["entries"]["automatic.model"]
                assert automatic["default_reasoning_effort"] == "auto"
                assert automatic["capabilities"]["context_window"] == 64000
                assert automatic["context_window"] == 64001
                assert automatic["output_limit"] == 2048
                assert automatic["images"] is False
                assert automatic["reasoning_efforts"] == ["low"]
                assert "execution_validated" not in automatic
                assert "synthetic-native-key" not in models.read_text()
                from dotenv import dotenv_values

                assert dotenv_values(root / ".env")[native_reference] == "synthetic-native-key"
                assert "discarded-secret" not in (root / ".env").read_text()
                assert saved["entries"]["primary"]["provider"] == "LeiHuo-OpenAI"
                assert "fallbacks" not in saved["entries"]["primary"]
                assert saved["entries"]["primary"]["reasoning_efforts"] == ["none", "medium"]
                # Explicit leave actions retain drafts on failure and save before navigation.
                await page.get_by_label("主对话模型", exact=True).select_option("primary")
                await page.get_by_role("button", name="对话与执行", exact=True).click()
                await expect(page.locator("#leave-dialog")).to_be_visible()
                await capture(page, "configuration-desktop-unsaved.png")

                async def fail_save(route):
                    await route.fulfill(status=409, json={"error": "synthetic conflict: reload required"})

                await page.route("**/api/configuration", fail_save, times=1)
                await page.get_by_role("button", name="保存并切换", exact=True).click()
                await expect(page.locator("#leave-dialog .dialog-notice")).to_contain_text(
                    "synthetic conflict"
                )
                await expect(page.locator("#leave-dialog")).to_be_visible()
                await page.get_by_role("button", name="继续编辑", exact=True).click()
                await expect(page.get_by_label("主对话模型", exact=True)).to_have_value("primary")
                await page.get_by_role("button", name="对话与执行", exact=True).click()
                await page.get_by_role("button", name="保存并切换", exact=True).click()
                await expect(page.locator("#page-title")).to_have_text("对话与执行")
                await expect(page.locator("#leave-dialog")).not_to_be_visible()
                await expect(page.get_by_label("共享工作额度", exact=True)).not_to_be_visible()
                fraction = page.get_by_label("用量预警比例", exact=True)
                await expect(fraction).to_have_value("50")
                await fraction.fill("100")
                await expect(page.locator("#save")).to_be_disabled()
                await expect(page.locator(".field-error:visible")).to_contain_text("小于 100")
                await fraction.fill("65")
                async with page.expect_response(
                    lambda r: r.url.endswith("/api/configuration") and r.request.method == "POST"
                ) as save_response:
                    await page.get_by_role("button", name="保存并生效", exact=True).click()
                saved_response = await save_response.value
                assert saved_response.status == 200, await saved_response.json()
                await expect(page.locator("#count")).to_have_text("没有未保存的修改")
                await expect(fraction).to_have_value("65")
                usage_file = next(
                    path for path in (root / "config").glob("*.toml") if "[ai_usage]" in path.read_text()
                )
                assert tomllib.loads(usage_file.read_text())["ai_usage"]["warning_fraction"] == 0.65
                await capture(page, "configuration-desktop-execution.png")
                await page.get_by_role("button", name="模型与账号", exact=True).click()
                await page.get_by_label("主对话模型", exact=True).select_option("fast")
                await page.get_by_role("button", name="实例管理", exact=True).click()
                await page.get_by_role("button", name="放弃并切换", exact=True).click()
                await page.get_by_role("button", name="模型与账号", exact=True).click()
                await expect(page.get_by_label("主对话模型", exact=True)).to_have_value("primary")
                for width, height in [(1024, 768), (1280, 800), (1366, 768), (1440, 900), (1920, 1080)]:
                    await page.set_viewport_size({"width": width, "height": height})
                    for name in ["功能与插件", "对话与执行", "模型与账号"]:
                        await page.get_by_role("button", name=name, exact=True).click()
                        assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                    await page.get_by_role("tab", name="模型库", exact=True).click()
                    await (
                        page.locator(".entity-card[data-entity=primary]")
                        .get_by_role("button", name="编辑", exact=True)
                        .click()
                    )
                    done = page.get_by_role("button", name="保留修改", exact=True)
                    bounds = await done.bounding_box()
                    assert bounds and bounds["y"] >= 0 and bounds["y"] + bounds["height"] <= height
                    assert await page.locator("#entry-dialog").evaluate("d => d.scrollWidth <= d.clientWidth")
                    await page.get_by_role("button", name="取消编辑", exact=True).click()
                await page.set_viewport_size({"width": 1440, "height": 1000})

                assert await page.locator("#apply-open, .restart-badge").count() == 0
                await page.evaluate('() => changed(["unit_name"], "set", "pa-browser-acceptance.service")')
                await page.get_by_role("button", name="保存并生效", exact=True).click()
                await expect(page.locator("#restart-dialog")).to_be_visible()
                await capture(page, "configuration-desktop-apply.png")
                await page.get_by_role("button", name="暂不重启", exact=True).click()
                assert (await (await page.request.get(origin + "/api/status")).json())["pid"] == pid
                await page.get_by_role("button", name="保存并生效", exact=True).click()
                await expect(page.locator("#restart-dialog")).to_be_visible()
                async with page.expect_response(
                    lambda r: r.url.endswith("/api/configuration/restart") and r.status == 200
                ):
                    await page.get_by_role("button", name="重启并生效", exact=True).click()
                async with asyncio.timeout(30):
                    while True:
                        applied = await (await page.request.get(origin + "/api/status")).json()
                        if applied["application"]["phase"] == "applied" and applied["pid"] != pid:
                            break
                        await asyncio.sleep(0.2)
                await expect(page.locator("#count")).to_have_text("没有未保存的修改")
                await page.get_by_role("button", name="变更记录", exact=True).click()
                await expect(page.locator("#history-dialog")).to_be_visible()
                assert await page.locator(".history-item").count() >= 1
                await page.get_by_role("button", name="关闭变更记录").click()
                await page.get_by_role("button", name="修改密码", exact=True).click()
                await expect(page.locator("#password-dialog")).to_be_visible()
                assert await page.locator("#new-password").get_attribute("minlength") == "6"
                await page.keyboard.press("Escape")
                await expect(page.locator("#password-dialog")).not_to_be_visible()
                await page.get_by_role("button", name="实例管理", exact=True).click()
                # Corrupt business configuration remains repairable through the independent shell.
                valid = entry.read_text()
                entry.write_text(valid + "broken = [\n")
                await page.get_by_role("button", name="重新加载", exact=True).click()
                await expect(page.locator("#repair-open")).to_be_visible()
                await page.get_by_role("button", name="配置修复", exact=True).click()
                await expect(page.locator("#repair-dialog")).to_be_visible()
                await page.get_by_label("需要修复的配置文件", exact=True).select_option("config.toml")
                await page.get_by_label("完整 TOML 内容", exact=True).fill(valid)
                await page.get_by_role("button", name="校验并修复", exact=True).click()
                await expect(page.locator("#notice")).to_have_text("已修复，待重启生效。")
                await page.get_by_role("button", name="修改密码", exact=True).click()
                await page.get_by_label("当前密码", exact=True).fill(
                    (root / "state/configuration/admin-token").read_text().strip()
                )
                await page.get_by_label("新密码", exact=True).fill("123456")
                await page.get_by_label("再次输入新密码", exact=True).fill("different")
                await page.get_by_role("button", name="保存新密码", exact=True).click()
                await expect(page.locator("#password-dialog .dialog-notice")).to_have_text(
                    "两次输入的新密码不一致。"
                )
                await page.get_by_label("再次输入新密码", exact=True).fill("123456")
                await page.get_by_role("button", name="保存新密码", exact=True).click()
                await expect(page.locator("#login-screen")).to_be_visible()
                await page.get_by_label("管理员密码", exact=True).fill("123456")
                await page.get_by_role("button", name="登录", exact=True).click()
                await expect(page.locator("#editor")).to_be_visible()
                assert (await (await page.request.get(origin + "/api/status")).json())["pid"] == applied[
                    "pid"
                ]
                assert not errors, errors
                await browser.close()
            print(
                json.dumps(
                    {
                        "desktop_1024_1280_1366_1440_1920": "passed",
                        "inline_validation_and_keyboard_tabs": "passed",
                        "search_and_empty_results": "passed",
                        "percentage_input_preserves_stored_fraction": "passed",
                        "leave_save_discard_and_failed_save": "passed",
                        "connection_wizard_and_model_tabs": "passed",
                        "editor_cancel_preserves_other_drafts": "passed",
                        "plugin_selection": "passed",
                        "core_and_extension_versions": "passed",
                        "provider_checks_and_model_selection": "passed",
                        "connection_auth_sources_and_exclusive_routes": "passed",
                        "nested_conditional_fields": "passed",
                        "invalid_draft_preservation": "passed",
                        "save_without_restart": "passed",
                        "model_alias_editing": "passed",
                        "new_provider_references": "passed",
                        "native_effort_checkboxes": "passed",
                        "automatic_model_capabilities": "passed",
                        "confirmed_restart": "passed",
                        "entry_removal": "passed",
                        "six_character_password_dialog": "passed",
                        "live_plugin_failure_and_recovery": "passed",
                        "apply_history_password_dialogs": "passed",
                        "broken_configuration_repair": "passed",
                        "javascript_errors": errors,
                    }
                )
            )
        except BaseException:
            try:
                if "page" in locals():
                    await capture(page, "configuration-desktop-failure.png", full_page=True)
            except Exception:
                pass
            print("Browser errors:", errors)
            print("Backend tail:", (root / "manager.log").read_text(errors="replace")[-1200:])
            raise
        finally:
            if process.returncode is None:
                process.terminate()
            await asyncio.wait_for(process.wait(), 80)
            log.close()


if __name__ == "__main__":
    asyncio.run(main())
