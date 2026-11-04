"""Browser acceptance for composer routing/layout using local synthetic responses."""

import argparse
import asyncio
import base64
import json
import re
from io import BytesIO
from pathlib import Path
import tempfile

from aiohttp import web
from playwright.async_api import async_playwright, expect
from PIL import Image
from assistant_runtime.core.models.builtin_avatars import encoded
from assistant_runtime.core.models.presentation import normalize, digest


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--browser", help="Use an existing Chromium executable")
    args = parser.parse_args()
    static = Path(__file__).resolve().parents[1] / "src/assistant_runtime/plugins/web/static"
    app = web.Application()
    app.router.add_get("/chat/", lambda request: web.FileResponse(static / "index.html"))
    app.router.add_static("/chat/", static)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    origin = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"

    def pending(ident, text, attachments=None):
        return dict(
            id=ident,
            state="queued",
            text=text,
            revision="v1-" + ident,
            message_id="web:" + ident,
            attachments=attachments or [],
        )

    def message(item, status="queued"):
        return dict(
            id=item["message_id"],
            ordinal=1,
            role="user",
            source="web",
            kind="message",
            text=item["text"],
            status=status,
            attachments=item["attachments"],
            updated=1,
        )

    fixture_messages = []
    image = BytesIO()
    Image.new("RGB", (32, 32), "green").save(image, format="PNG")
    avatar_png = normalize(image.getvalue())
    avatar_id = digest(avatar_png)
    public_model = dict(
        model="model-a",
        display_name="Public Model",
        description="Public description",
        avatar=dict(kind="image", asset_id=avatar_id, version=avatar_id),
    )
    avatar_missing = False
    state = dict(
        name="Personal Agent",
        voice="disabled",
        speaking=False,
        task_running=True,
        quiet=False,
        session_id="test",
        web_dispatching=False,
        active_request_id="active-one",
        questions=[],
        queue=[
            pending("queued-two", "整理接口说明并补充示例"),
            pending(
                "queued-three",
                "核对移动端的输入体验与消息顺序",
                [{"id": "queued-image", "name": "image.jpg"}],
            ),
            pending("queued-four", "请检查消息队列和输入框在不同屏幕下的布局，保留完整内容且一行展示。" * 5),
        ],
    )
    posts, errors = [], []
    failure = {"next": False, "queue": False}
    output = Path(tempfile.mkdtemp(prefix="pa-composer-ui-"))
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(
                **({"executable_path": args.browser} if args.browser else {}), headless=True
            )
            page = await browser.new_page(
                viewport={"width": 1366, "height": 900}, color_scheme="dark", has_touch=True
            )
            page.on("pageerror", lambda error: errors.append(str(error)))
            await page.add_init_script("""window.EventSource = class extends EventTarget {
                constructor() { super(); window.fixtureEvents = this; setTimeout(() => this.onopen?.(), 0); }
                close() {}
            };""")

            async def route_api(route):
                path = route.request.url.split("/api/")[1].split("?")[0]
                data = {}
                changed = False
                if route.request.method == "POST":
                    if path == "uploads":
                        data = {"id": "image-one", "name": "test.png"}
                    else:
                        body = route.request.post_data_json
                        posts.append((path, body))
                        action = body.get("action")
                        if path == "conversation" and failure["next"]:
                            failure["next"] = False
                            await route.fulfill(
                                status=409, json={"error": "当前执行已结束，请确认后重新发送。"}
                            )
                            return
                        if path == "messages":
                            item = pending(
                                body["request_id"],
                                body["text"],
                                [{"id": ident, "name": "test.png"} for ident in body["attachments"]],
                            )
                            state["queue"].append(item)
                            data = {"message": message(item), "state": "queued"}
                            changed = True
                        elif path == "conversation" and action in {
                            "edit_queued",
                            "remove_queued",
                            "reorder_queue",
                        }:
                            if failure["queue"]:
                                failure["queue"] = False
                                await route.fulfill(
                                    status=409, json={"error": "队列已变化，请核对后再操作。"}
                                )
                                return
                            if action == "reorder_queue":
                                queued = [r for r in state["queue"] if r["state"] == "queued"]
                                assert body["expected_order"] == [r["id"] for r in queued]
                                assert set(body["request_ids"]) == {r["id"] for r in queued}
                                lookup = {r["id"]: r for r in queued}
                                state["queue"] = [r for r in state["queue"] if r["state"] != "queued"] + [
                                    lookup[i] for i in body["request_ids"]
                                ]
                            else:
                                item = next(r for r in state["queue"] if r["id"] == body["request_id"])
                                assert item["state"] == "queued" and item["revision"] == body["revision"]
                                if action == "edit_queued":
                                    item["text"] = body["text"]
                                    item["revision"] += "-edited"
                                    data["message"] = message(item)
                                else:
                                    state["queue"] = [r for r in state["queue"] if r["id"] != item["id"]]
                                    data["message"] = message(item, "cancelled")
                            data["requests"] = state["queue"]
                            changed = True
                elif path == "messages":
                    data = {"messages": fixture_messages, "state": state, "cursor": 0, "before": None}
                elif path == "session":
                    data = state
                elif path == "models":
                    data = {"models": [public_model], "default": {"route": {"model": "model-a"}}}
                elif path.startswith("model-avatar/"):
                    if avatar_missing:
                        await route.fulfill(status=404)
                    elif "/builtin/" in path:
                        icon = encoded(path.rsplit("/", 1)[1])
                        await route.fulfill(
                            content_type=icon["media_type"], body=base64.b64decode(icon["data_base64"])
                        )
                    else:
                        await route.fulfill(content_type="image/png", body=avatar_png)
                    return
                elif path.startswith("uploads/"):
                    await route.fulfill(
                        content_type="image/png",
                        body=base64.b64decode(
                            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aPFsAAAAASUVORK5CYII="
                        ),
                    )
                    return
                await route.fulfill(json=data)
                if changed:
                    await update()

            await page.route("**/api/**", route_api)
            await page.goto(origin + "/chat/")
            entry = page.get_by_role("textbox", name="消息", exact=True)

            switch = page.get_by_role("switch", name="直接引导", exact=True)

            async def choose(mode):
                expected = "true" if mode == "直接引导" else "false"
                if await switch.get_attribute("aria-checked") != expected:
                    await switch.click()
                await expect(switch).to_have_attribute("aria-checked", expected)
                await expect(page.get_by_role("menu")).to_have_count(0)

            async def update():
                await page.evaluate(
                    "s => window.fixtureEvents.dispatchEvent(new MessageEvent('update', {data: JSON.stringify({seq: 1, kind: 'state', data: s})}))",
                    state,
                )

            # A fresh browser directs the active execution; sending never changes the preference.
            await expect(page.get_by_role("button", name="直接引导", exact=True)).to_be_disabled()
            await expect(page.locator(".composer-context")).to_have_count(0)
            await expect(page.locator(".delivery-modes")).to_have_count(0)
            await expect(switch).to_have_text("引导")
            assert await switch.evaluate("e => !!e.closest('.composer-actions')")
            queue_box = await page.locator(".composer-queue").bounding_box()
            composer_box = await page.locator(".composer").bounding_box()
            assert queue_box["y"] + queue_box["height"] <= composer_box["y"]
            await entry.fill("请把这项要求也考虑进去")
            await page.locator(".composer-wrap").screenshot(path=str(output / "running-dark.png"))
            await entry.press("Enter")
            await expect(entry).to_have_value("")
            assert posts[-1] == (
                "conversation",
                {"action": "supplement", "request_id": "active-one", "text": "请把这项要求也考虑进去"},
            )
            await expect(page.get_by_role("button", name="添加图片", exact=True)).to_be_disabled()
            await choose("排队")
            await expect(switch).to_have_text("排队")
            await entry.fill("下一轮处理这件事")
            await page.reload()
            await expect(entry).to_have_value("下一轮处理这件事")
            await expect(switch).to_have_attribute("aria-checked", "false")
            await entry.press("Enter")
            await expect(entry).to_have_value("")
            assert posts[-1][0] == "messages"

            # Space and Enter switch in place, without opening a menu.
            await switch.press("Space")
            await expect(switch).to_have_attribute("aria-checked", "true")
            await page.reload()
            await expect(switch).to_have_attribute("aria-checked", "true")
            await switch.press("Enter")
            await expect(switch).to_have_attribute("aria-checked", "false")
            await switch.press("Enter")
            await expect(switch).to_have_attribute("aria-checked", "true")
            await expect(page.get_by_role("menu")).to_have_count(0)

            state.update(
                question_id="question-one",
                questions=[{"id": "question-one", "question": "先完成方案设计，还是直接开始实现？"}],
            )
            await update()
            await entry.fill("先完成方案设计")
            await page.locator(".composer-wrap").screenshot(path=str(output / "question-dark.png"))
            await page.get_by_role("button", name="发送回答", exact=True).click()
            await expect(entry).to_have_value("")
            assert posts[-1][1]["question_id"] == "question-one"
            assert posts[-1][1]["action"] == "answer"
            await expect(switch).to_be_enabled()

            # An execution change targets the new execution without resetting the choice.
            state.update(active_request_id="active-next", question_id=None, questions=[])
            await update()
            await entry.fill("调整新执行的方向")
            await entry.press("Enter")
            await expect(entry).to_have_value("")
            assert posts[-1][1]["request_id"] == "active-next"
            assert posts[-1][1]["action"] == "supplement"
            failure["next"] = True
            await entry.fill("不要丢失这份补充")
            count = len(posts)
            await entry.press("Enter")
            await expect(page.get_by_role("alert")).to_contain_text("当前执行已结束")
            await expect(entry).to_have_value("不要丢失这份补充")
            assert len(posts) == count + 1 and posts[-1][0] == "conversation"
            await expect(switch).to_have_attribute("aria-checked", "true")

            await entry.fill("保留这份草稿")
            rows = page.locator(".queue-row")

            def row(ident):
                return page.locator(f'.queue-row[data-queue-id="{ident}"]')

            # Drag and keyboard ordering send the complete pending snapshot, never a new message.
            await (
                row("queued-four")
                .locator(".queue-grip")
                .drag_to(row("queued-two"), target_position={"x": 180, "y": 3})
            )
            await expect(rows.first).to_have_attribute("data-queue-id", "queued-four")
            assert posts[-1][1]["action"] == "reorder_queue"
            await page.get_by_role("button", name="下移第 1 条排队消息", exact=True).focus()
            await page.get_by_role("button", name="下移第 1 条排队消息", exact=True).press("Enter")
            await expect(rows.first).to_have_attribute("data-queue-id", "queued-two")
            assert posts[-1][1]["request_ids"][:2] == ["queued-two", "queued-four"]

            # Editing is inline; neither a rejected edit nor a successful edit touches the composer draft.
            await row("queued-two").get_by_role("button", name=re.compile("^编辑第")).click()
            editor = page.get_by_role("textbox", name="修改排队消息", exact=True)
            await expect(editor).to_have_value("整理接口说明并补充示例")
            await editor.fill("更新后的接口说明\n保留第二行")
            failure["queue"] = True
            count = len(posts)
            await page.get_by_role("button", name="保存排队消息修改", exact=True).click()
            await expect(page.get_by_role("alert")).to_contain_text("队列已变化")
            await expect(editor).to_have_value("更新后的接口说明\n保留第二行")
            assert len(posts) == count + 1
            await expect(entry).to_have_value("保留这份草稿")
            await editor.press("Enter")
            await expect(editor).to_have_count(0)
            await expect(row("queued-two").locator(".queue-text")).to_have_text(
                "更新后的接口说明\n保留第二行"
            )
            assert posts[-1][1]["action"] == "edit_queued"
            await expect(page.locator('.conversation [data-message-id="web:queued-two"]')).to_have_count(0)

            await row("queued-three").get_by_role("button", name=re.compile("^删除第")).click()
            await expect(row("queued-three")).to_have_count(0)
            await expect(entry).to_have_value("保留这份草稿")
            assert posts[-1][1]["action"] == "remove_queued" and posts[-1][1]["request_id"] == "queued-three"
            await entry.focus()
            await page.locator(".composer-wrap").screenshot(path=str(output / "queue-dark.png"))

            # If execution wins the race, keep the unsaved text and offer an explicit draft recovery.
            await row("queued-two").get_by_role("button", name=re.compile("^编辑第")).click()
            await editor.fill("尚未保存的队列修改")
            next(r for r in state["queue"] if r["id"] == "queued-two")["state"] = "running"
            await update()
            await expect(page.locator(".queue-edit-note")).to_contain_text("已开始执行或已移出队列")
            await expect(editor).to_have_value("尚未保存的队列修改")
            await expect(page.get_by_role("button", name="保存排队消息修改", exact=True)).to_have_count(0)
            count = len(posts)
            await page.get_by_role("button", name="复制到输入框", exact=True).click()
            await expect(entry).to_have_value("保留这份草稿\n尚未保存的队列修改")
            assert len(posts) == count
            await entry.fill("保留这份草稿")
            await page.get_by_role("button", name="停止当前执行", exact=True).click()
            await expect(page.get_by_role("button", name="停止当前执行", exact=True)).to_be_enabled()
            assert posts[-1][1] == {"action": "cancel", "request_id": "active-next"}
            await expect(entry).to_have_value("保留这份草稿")
            await entry.press("Shift+Enter")
            await expect(entry).to_have_value("保留这份草稿\n")

            # An attached image never silently changes a steering message into queued work.
            await choose("排队")
            await page.locator('input[type="file"]').set_input_files(
                {
                    "name": "test.png",
                    "mimeType": "image/png",
                    "buffer": base64.b64decode(
                        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aPFsAAAAASUVORK5CYII="
                    ),
                }
            )
            await expect(page.get_by_role("button", name="移除 test.png", exact=True)).to_be_visible()
            await choose("直接引导")
            await expect(page.get_by_role("button", name="直接引导", exact=True)).to_be_disabled()
            await expect(page.locator(".composer-delivery-note")).to_be_visible()
            count = len(posts)
            await entry.press("Enter")
            assert len(posts) == count
            await expect(entry).to_have_value("保留这份草稿\n")
            await choose("排队")
            await entry.press("Enter")
            await expect(entry).to_have_value("")
            assert posts[-1][0] == "messages" and posts[-1][1]["attachments"] == ["image-one"]
            await choose("直接引导")
            await entry.fill("继续当前执行")
            await page.emulate_media(color_scheme="light")
            await page.locator(".composer-wrap").screenshot(path=str(output / "running-light.png"))
            running_height = await page.locator(".composer").evaluate("e => e.offsetHeight")
            state.update(active_request_id=None, task_running=False, queue=[])
            await update()
            await expect(page.get_by_role("button", name="发送", exact=True)).to_be_enabled()
            idle_height = await page.locator(".composer").evaluate("e => e.offsetHeight")
            assert running_height == idle_height, (running_height, idle_height)
            await page.locator(".composer-wrap").screenshot(path=str(output / "idle-light.png"))
            await entry.press("Enter")
            await expect(entry).to_have_value("")
            assert posts[-1][0] == "messages"
            state.update(active_request_id="active-next", task_running=True)
            await update()
            for width in (320, 375, 600, 1024, 1366, 1920):
                await page.set_viewport_size({"width": width, "height": 900})
                await switch.tap()
                await expect(switch).to_have_attribute("aria-checked", "false")
                await switch.tap()
                await expect(switch).to_have_attribute("aria-checked", "true")
                await expect(page.get_by_role("menu")).to_have_count(0)
                assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth"), width
                if width == 375:
                    await page.screenshot(path=str(output / "mobile-light-switch.png"))
            await page.evaluate("window.fixtureEvents.onerror()")
            await expect(page.get_by_role("button", name="直接引导", exact=True)).to_be_disabled()
            await expect(page.get_by_role("button", name="停止当前执行", exact=True)).to_be_disabled()
            state.update(active_request_id=None, task_running=False, web_dispatching=False, queue=[])
            fixture_messages.extend(
                [
                    dict(
                        id="failed-user",
                        ordinal=1,
                        role="user",
                        source="web",
                        kind="message",
                        text="合成恢复测试",
                        status="complete",
                        attachments=[],
                        updated=1,
                        turn_id="failed-turn",
                    ),
                    dict(
                        id="failed-model",
                        ordinal=2,
                        role="assistant",
                        source="task",
                        kind="message",
                        text="暂时繁忙",
                        status="complete",
                        attachments=[],
                        updated=2,
                        turn_id="failed-turn",
                        failure=dict(
                            code="model_transient",
                            category="transient",
                            status=503,
                            message="Synthetic upstream overloaded",
                            request_id="synthetic-request",
                            retry_allowed=True,
                            stop_reason="attempts_exhausted",
                            attempts=[dict(attempt=1, elapsed_ms=120, category="transient", status=503)],
                        ),
                    ),
                ]
            )
            await page.set_viewport_size({"width": 1366, "height": 900})
            await page.reload()
            retry = page.get_by_role("button", name="重试", exact=True)
            await expect(retry).to_be_enabled()
            await expect(page.get_by_text("Synthetic upstream overloaded")).to_be_visible()
            await page.locator(".model-failure summary").click()
            await expect(page.get_by_text("synthetic-request", exact=True)).to_be_visible()
            failure["next"] = True
            await retry.click()
            await expect(page.locator(".model-failure [role='alert']")).to_be_visible()
            retry_id = posts[-1][1]["request_id"]
            await retry.click()
            await expect(page.get_by_role("button", name="已提交继续执行")).to_be_disabled()
            assert posts[-1][1] == dict(action="retry", turn_id="failed-turn", request_id=retry_id)
            assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            await page.screenshot(path=str(output / "model-failure-recovery.png"))
            fixture_messages[:] = [
                dict(
                    id="personalized-reply",
                    ordinal=10,
                    role="assistant",
                    source="web",
                    kind="message",
                    text="Personalized reply",
                    status="complete",
                    attachments=[],
                    updated=1,
                    turn_id="presentation-turn",
                )
            ]
            state.update(task_running=False, active_request_id=None, active_turn_id=None, queue=[])
            await page.reload()
            speaker = page.locator(".message-group.assistant .speaker")
            avatar = page.locator(".message-group.assistant .model-avatar img")
            await expect(speaker).to_have_text("Public Model")
            await expect(speaker).to_have_attribute("title", "Public description")
            await expect(avatar).to_have_js_property("naturalWidth", 32)
            public_model.update(display_name="Updated Model", avatar=dict(kind="builtin", name="deepseek"))
            await page.reload()
            await expect(speaker).to_have_text("Updated Model")
            await expect(avatar).to_have_attribute("src", "/chat/api/model-avatar/builtin/deepseek")
            await expect(avatar).to_have_js_property("naturalWidth", 64)
            await page.screenshot(path=str(output / "model-presentation-reloaded.png"))
            avatar_missing = True
            await page.reload()
            await expect(speaker).to_have_text("Updated Model")
            await expect(avatar).to_have_count(0)
            await expect(page.locator(".message-group.assistant .model-avatar svg")).to_be_visible()
            assert not errors, errors
            await browser.close()
    finally:
        await runner.cleanup()
    print(
        json.dumps(
            {
                "composer_default_steer_persistence_routing_keyboard_attachments_disconnect": "passed",
                "queue_display_drag_keyboard_edit_remove_conflict_recovery": "passed",
                "desktop_mobile_layout": "passed",
                "model_failure_details_retry_idempotent_request": "passed",
                "model_presentation_reload_uploaded_builtin_and_missing_avatar": "passed",
                "screenshots": str(output),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
