"""Isolated desktop console acceptance: synthetic files, no model or device calls.

Requires Playwright, its Chromium browser and the installed sample plugin. Use a
separate validation environment, never install test dependencies into a running service.
"""

import argparse
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

    with tempfile.TemporaryDirectory(prefix="pa-desktop-console-") as folder:
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
        (root / ".env").write_text("DEEPSEEK_API_KEY=synthetic-secret-before\n")
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
                await capture(page, "configuration-desktop-access.png")
                await page.get_by_role("button", name="插件", exact=True).click()
                sample = page.locator(".plugin-option").filter(has_text="示例笔记").locator("input")
                if sys.platform == "linux":
                    await sample.uncheck()
                    await sample.check()
                    await page.get_by_role("button", name="放弃修改", exact=True).click()
                else:
                    await expect(sample).to_be_disabled()
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
                await page.get_by_role("button", name="实例管理", exact=True).click()
                await expect(maximum).to_have_value("0")
                await maximum.fill("1200")
                await page.get_by_label("报告风格", exact=True).select_option("detailed")
                await expect(page.get_by_label("报告风格", exact=True)).to_have_value("detailed")
                await page.get_by_label("标题", exact=True).fill("执行报告")
                async with page.expect_response(
                    lambda r: r.url.endswith("/api/configuration")
                    and r.request.method == "POST"
                    and r.status == 200
                ):
                    await page.get_by_role("button", name="保存并生效", exact=True).click()
                await expect(page.locator("#notice")).to_have_text(
                    "已保存，正在应用；需要等待或重启的项目会显示具体状态。"
                )
                config = tomllib.loads(entry.read_text())
                assert config["plugin_settings"]["sample"]["report"]["style"] == "detailed"
                assert config["plugin_settings"]["sample"]["max_length"] == 1200
                assert (await (await page.request.get(origin + "/api/status")).json())["pid"] == pid
                await page.get_by_role("button", name="模型与账号", exact=True).click()
                providers = page.locator(selector(["model_api", "providers"]))
                await providers.get_by_role("textbox", name="2. 连接供应商 · 新条目名称", exact=True).fill(
                    "testing"
                )
                await providers.get_by_role("button", name="添加条目", exact=True).click()
                testing = (
                    providers.locator(".named-entry")
                    .filter(has=page.get_by_text("testing", exact=True))
                    .first
                )
                await testing.get_by_label("预设接口", exact=True).select_option("deepseek")
                await providers.get_by_role("textbox", name="2. 连接供应商 · 新条目名称", exact=True).fill(
                    "discard_me"
                )
                await providers.get_by_role("button", name="添加条目", exact=True).click()
                discarded = (
                    providers.locator(".named-entry")
                    .filter(has=page.get_by_text("discard_me", exact=True))
                    .first
                )
                await discarded.get_by_role("button", name="移除条目", exact=True).click()
                await expect(providers.get_by_text("discard_me", exact=True)).to_have_count(0)
                primary = (
                    page.locator(selector(["model_api", "entries"]))
                    .locator(".named-entry")
                    .filter(has=page.get_by_text("primary", exact=True))
                    .first
                )
                await primary.locator(":scope > summary").click()
                await primary.get_by_label("供应商", exact=True).select_option("testing")
                await expect(primary.get_by_label("供应商", exact=True)).to_have_value("testing")
                await primary.get_by_text("高级设置", exact=True).click()
                efforts = primary.get_by_role("group", name="模型支持的思考深度", exact=True)
                await efforts.locator("input[value=none]").check()
                await efforts.locator("input[value=high]").uncheck()
                fallback = primary.locator(selector(["model_api", "entries", "primary", "fallbacks"]))
                await fallback.get_by_role("button", name="添加一项", exact=True).click()
                await fallback.get_by_label("有序回退模型 · 第 1 项", exact=True).select_option("fast")
                await capture(page, "configuration-desktop-models.png")
                async with page.expect_response(
                    lambda r: r.url.endswith("/api/configuration")
                    and r.request.method == "POST"
                    and r.status == 200
                ):
                    await page.get_by_role("button", name="保存并生效", exact=True).click()
                await expect(page.locator("#notice")).to_have_text(
                    "已保存，正在应用；需要等待或重启的项目会显示具体状态。"
                )
                saved = tomllib.loads(models.read_text())["model_api"]
                assert saved["entries"]["primary"]["provider"] == "testing"
                assert saved["entries"]["primary"]["fallbacks"] == ["fast"]
                assert saved["entries"]["primary"]["reasoning_efforts"] == ["none", "medium"]
                for width, height in [(1280, 800), (1920, 1080)]:
                    await page.set_viewport_size({"width": width, "height": height})
                    assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                await page.set_viewport_size({"width": 1440, "height": 1000})
                await page.get_by_role("button", name="生效状态与维护", exact=True).click()
                await expect(page.locator("#apply-dialog")).to_be_visible()
                await capture(page, "configuration-desktop-apply.png")
                async with page.expect_response(lambda r: r.url.endswith("/api/apply") and r.status == 202):
                    await page.get_by_role("button", name="等待完成后重启", exact=True).click()
                async with asyncio.timeout(30):
                    while True:
                        applied = await (await page.request.get(origin + "/api/status")).json()
                        if applied["application"]["phase"] == "applied" and applied["pid"] != pid:
                            break
                        await asyncio.sleep(0.2)
                await page.get_by_role("button", name="关闭应用配置").click()
                await page.get_by_role("button", name="变更记录", exact=True).click()
                await expect(page.locator("#history-dialog")).to_be_visible()
                assert await page.locator(".history-item").count() >= 1
                await page.get_by_role("button", name="关闭变更记录").click()
                await page.get_by_role("button", name="修改密码", exact=True).click()
                await expect(page.locator("#password-dialog")).to_be_visible()
                assert await page.locator("#new-password").get_attribute("minlength") == "7"
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
                await page.get_by_label("新密码", exact=True).fill("1234567")
                await page.get_by_label("再次输入新密码", exact=True).fill("different")
                await page.get_by_role("button", name="保存新密码", exact=True).click()
                await expect(page.locator("#password-dialog .dialog-notice")).to_have_text(
                    "两次输入的新密码不一致。"
                )
                await page.get_by_label("再次输入新密码", exact=True).fill("1234567")
                await page.get_by_role("button", name="保存新密码", exact=True).click()
                await expect(page.locator("#login-screen")).to_be_visible()
                await page.get_by_label("管理员密码", exact=True).fill("1234567")
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
                        "desktop_1280_1440_1920": "passed",
                        "plugin_selection": "passed",
                        "nested_conditional_fields": "passed",
                        "invalid_draft_preservation": "passed",
                        "save_without_restart": "passed",
                        "model_alias_editing": "passed",
                        "new_provider_references": "passed",
                        "ordered_fallbacks_and_effort_checkboxes": "passed",
                        "confirmed_restart": "passed",
                        "entry_removal": "passed",
                        "seven_character_password_dialog": "passed",
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
