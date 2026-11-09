"""Explicit live acceptance: silent jobs, native Pi files/goals, foreground and cancellation."""

import argparse
import asyncio
import json
from pathlib import Path
import time
import uuid

from assistant_runtime.app.config import Config
from assistant_runtime.api.contracts import TaskRequest
from assistant_runtime.mcp.client import LocalRuntimeClient


async def verify(config):
    client = LocalRuntimeClient(config.socket)
    evidence = {"started": time.time(), "checks": {}}
    created = []

    async def call(action, **args):
        return await client.invoke(TaskRequest(action, args))

    async def create(title, prompt, **extra):
        definition = await call(
            "create",
            request_id=str(uuid.uuid4()),
            spec={"title": "验收 · " + title, "prompt": prompt, **extra},
        )
        created.append(definition["id"])
        return definition

    async def wait(definition, states, timeout=150):
        until = time.monotonic() + timeout
        while time.monotonic() < until:
            info = await call("get", task_id=definition["id"])
            if info["runs"] and info["runs"][0]["state"] in states:
                return info["runs"][0]
            await asyncio.sleep(0.5)
        raise AssertionError("Timed out waiting for " + definition["spec"]["title"])

    try:
        status = await client.status()
        if status.get("task_running") or status.get("mode") != "standby":
            raise RuntimeError("Live acceptance requires idle foreground")
        before_session = status["session_id"]
        catalog = (await call("models"))["models"]
        selected = next((m for m in catalog if m["model"] == config.main_model), catalog[0])
        efforts = [e["reasoning_effort"] for e in selected["supported_reasoning_efforts"]]
        execution = {
            "model": selected["model"],
            "reasoning_effort": "low" if "low" in efforts else efforts[0],
            "reason": "bounded acceptance task",
        }
        evidence["execution"] = execution
        reminder = await create("静默提醒", "任务插件静默提醒验收。", execution={"kind": "reminder"})
        r = await wait(reminder, {"completed", "failed", "unknown"})
        assert r["state"] == "completed", r
        evidence["checks"]["silent_reminder"] = r["id"]
        for goal in (False, True):
            marker = "task-acceptance-" + uuid.uuid4().hex
            d = await create(
                "Goal 文件" if goal else "独立文件",
                f"这是一次本地接入验收。只在开发者指令给定的本任务 artifacts 目录创建 result.txt，内容为 {marker}，读回确认后简短报告结果。不要操作其他文件、设备或创建子任务。"
                + (" 完成后用 update_task_goal 工具标记目标完成。" if goal else ""),
                execution=execution,
                long_term=goal,
            )
            r = await wait(d, {"completed", "failed", "unknown", "waiting_user"})
            assert r["state"] == "completed", r
            root = Path(config.work_files.get("root", config.state_dir / "work"))
            artifact = root / "tasks" / d["id"] / r["id"] / "artifacts/result.txt"
            assert artifact.read_text().strip() == marker
            assert r["data"]["execution_session_id"] != status.get("execution_session_id")
            evidence["checks"]["goal_file" if goal else "independent_file"] = {
                "run": r["id"],
                "session": r["data"]["execution_session_id"],
                "duration": r["updated"] - r["created"],
            }
        d = await create(
            "取消执行",
            "这是取消能力验收。启动一个最多60秒的等待命令（sleep 60），不要修改任何文件，不要创建子任务；等待结束后才报告完成。",
            execution=execution,
        )
        r = await wait(d, {"running", "failed", "unknown"}, timeout=60)
        assert r["state"] == "running", r
        await asyncio.sleep(4)
        await call("supplement", run_id=r["id"], text="验收补充：结束后只回答等待已结束，不要增加任何操作。")
        evidence["checks"]["supplement"] = True
        if config.plugin_settings["web"].get("enable"):
            import aiohttp
            from assistant_runtime.plugins.web.settings import WebSettings

            ws = WebSettings.parse(config.plugin_settings["web"])
            from assistant_runtime.core.configuration.settings import ManagementSettings

            management = ManagementSettings.load(config.root / "management.toml")
            origin = f"http://127.0.0.1:{management.port}"
            base = origin + "/chat"
            credential = ws.credential_path(config.state_dir).read_text().strip()
            async with aiohttp.ClientSession(
                cookie_jar=aiohttp.CookieJar(unsafe=True), headers={"Origin": origin}
            ) as session:
                async with session.post(base + "/api/login", json={"token": credential}) as response:
                    assert response.status == 200
                async with session.post(
                    base + "/api/messages",
                    json={
                        "request_id": str(uuid.uuid4()),
                        "text": "这是自动验收，只需回答 2+3 等于多少，不创建任务、不调用工具。",
                        "attachments": [],
                    },
                ) as response:
                    assert response.status == 202
                until = time.monotonic() + 45
                while time.monotonic() < until:
                    async with session.get(base + "/api/messages") as response:
                        history = await response.json()
                    if any(
                        m["role"] == "assistant"
                        and m["source"] != "background"
                        and m["updated"] > r["created"]
                        and "5" in m["text"]
                        and m["status"] == "complete"
                        for m in history["messages"]
                    ):
                        evidence["checks"]["foreground_during_background"] = True
                        break
                    await asyncio.sleep(0.5)
                assert evidence["checks"].get("foreground_during_background"), "Foreground response missing"
        await call("cancel_run", run_id=r["id"])
        current = (await call("get", task_id=d["id"]))["runs"][0]
        assert current["state"] == "canceled", current
        evidence["checks"]["cancel"] = r["id"]
        after = await client.status()
        assert after["session_id"] == before_session
        assert after["mode"] == "standby"
        evidence["checks"]["preserved_foreground_and_standby"] = True
    finally:
        for ident in created:
            try:
                await call("archive", task_id=ident)
            except Exception as exc:
                evidence.setdefault("cleanup_errors", []).append(type(exc).__name__)
        target = config.state_dir / "acceptance/tasks"
        target.mkdir(parents=True, exist_ok=True)
        (target / "live.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2))
    print(json.dumps(evidence, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    asyncio.run(verify(Config.load(parser.parse_args().config)))
