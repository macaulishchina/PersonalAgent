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
                    lambda r: r.url.endswith("/api/configuration")
                    and r.request.method == "POST"
                    and r.status == 200
                ):
                    await page.get_by_role("button", name="保存并生效", exact=True).click()
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
                await page.get_by_role("tab", name="连接", exact=True).focus()
                await page.keyboard.press("ArrowRight")
                await expect(page.get_by_role("tab", name="模型库", exact=True)).to_be_focused()
                await page.keyboard.press("ArrowLeft")
                await expect(page.get_by_role("tab", name="连接", exact=True)).to_be_focused()
                await page.get_by_role("button", name="添加连接", exact=True).click()
                setup = page.locator("#setup-dialog")
                await capture(page, "configuration-desktop-connection-wizard.png")
                await setup.get_by_role("radio", name="通过 API 网关", exact=True).check()
                await setup.get_by_label("连接名称", exact=True).fill("testing")
                await setup.get_by_label("接口类型", exact=True).select_option("deepseek")
                await setup.get_by_role("button", name="添加连接", exact=True).click()
                await expect(page.locator(".entity-card[data-entity=testing]")).to_be_visible()
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
                await setup.get_by_role("radio", name="通过 API 网关", exact=True).check()
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

                await (
                    page.locator(".entity-card[data-entity=primary]")
                    .get_by_role("button", name="编辑", exact=True)
                    .click()
                )
                primary = page.locator("#entry-dialog")
                await primary.get_by_label("使用连接", exact=True).select_option("testing")
                await expect(primary.get_by_label("使用连接", exact=True)).to_have_value("testing")
                await primary.get_by_text("高级设置", exact=True).click()
                efforts = primary.get_by_role("group", name="模型支持的思考深度", exact=True)
                await efforts.locator("input[value=none]").check()
                await efforts.locator("input[value=high]").uncheck()
                fallback = primary.locator(selector(["model_api", "entries", "primary", "fallbacks"]))
                await fallback.get_by_role("button", name="添加一项", exact=True).click()
                await fallback.get_by_label("有序回退模型 · 第 1 项", exact=True).select_option("fast")
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
                    lambda r: r.url.endswith("/api/configuration")
                    and r.request.method == "POST"
                    and r.status == 200
                ):
                    await page.get_by_role("button", name="保存并生效", exact=True).click()
                await expect(page.locator("#notice")).to_have_text(
                    "已保存，正在应用；需要等待或重启的项目会显示具体状态。"
                )
                saved = tomllib.loads(models.read_text())["model_api"]
                assert saved["providers"]["native_test"]["account"] == "native_test"
                assert saved["accounts"]["native_test"]["key_env"] == "PA_NATIVE_TEST_API_KEY"
                assert "cancelled_connection" not in saved["providers"]
                assert "cancelled_model" not in saved["entries"]
                assert "synthetic-native-key" not in models.read_text()
                from dotenv import dotenv_values

                assert dotenv_values(root / ".env")["PA_NATIVE_TEST_API_KEY"] == "synthetic-native-key"
                assert "discarded-secret" not in (root / ".env").read_text()
                assert saved["entries"]["primary"]["provider"] == "testing"
                assert saved["entries"]["primary"]["fallbacks"] == ["fast"]
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
                    lambda r: r.url.endswith("/api/configuration")
                    and r.request.method == "POST"
                    and r.status == 200
                ):
                    await page.get_by_role("button", name="保存并生效", exact=True).click()
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
                        "desktop_1024_1280_1366_1440_1920": "passed",
                        "inline_validation_and_keyboard_tabs": "passed",
                        "search_and_empty_results": "passed",
                        "percentage_input_preserves_stored_fraction": "passed",
                        "leave_save_discard_and_failed_save": "passed",
                        "connection_wizard_and_model_tabs": "passed",
                        "editor_cancel_preserves_other_drafts": "passed",
                        "plugin_selection": "passed",
                        "core_and_extension_versions": "passed",
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
