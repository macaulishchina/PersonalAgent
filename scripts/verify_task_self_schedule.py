"""Explicit installed-service acceptance of self scheduling, without devices or speech."""

import argparse
import asyncio
import json
from pathlib import Path
import time
import uuid

from assistant_runtime.config import Config
from assistant_runtime.kernel.contracts import TaskRequest
from assistant_runtime.mcp.client import LocalRuntimeClient


async def verify(config):
    client = LocalRuntimeClient(config.socket)
    result = {"synthetic": True, "started_at": time.time(), "passed": False}
    definition = None

    async def call(action, **args):
        return await client.invoke(TaskRequest(action, args))

    try:
        status = await client.status()
        if status["mode"] != "standby" or status["task_running"]:
            raise RuntimeError("Self scheduling acceptance requires idle foreground")
        definition = await call(
            "create",
            request_id=str(uuid.uuid4()),
            spec={
                "title": "验收 · 自身计划调整",
                "prompt": "这是框架一次合成验收。不要使用shell、记忆、设备、语音，不创建子任务。开发者指令给出你的task_id；先用get_task读自己的最新任务版本和self_schedule授权。验收条件现在已满足：本任务就是明确授权的调度测试。请用update_own_task_schedule把自身trigger改成{kind:interval,seconds:86400}，使用一个新规范UUID，附上简短reason和evidence=本次用户授权的合成调度验收。读回自己的计划确认trigger为86400并且next_at在将来，简短结束。不修改任何其他字段或任务。",
                "trigger": {"kind": "interval", "seconds": 1800},
                "execution": {
                    "model": config.main_model,
                    "reasoning_effort": "low",
                    "reason": "明确工具和验收条件的合成接入测试",
                },
                "notify_completion": False,
                "notify_voice": False,
                "self_schedule": {
                    "allowed_triggers": [{"kind": "interval", "seconds": 86400}],
                    "condition": "Only this explicitly authorized synthetic scheduling acceptance.",
                },
            },
        )
        result["task_id"] = definition["id"]
        until = time.monotonic() + 180
        info = None
        while time.monotonic() < until:
            info = await call("get", task_id=definition["id"])
            if info["runs"] and info["runs"][0]["state"] in {
                "completed",
                "failed",
                "unknown",
                "waiting_user",
            }:
                break
            await asyncio.sleep(0.5)
        if not info or not info["runs"]:
            raise RuntimeError("Synthetic execution not observed; do not replay")
        run = info["runs"][0]
        result.update(run_id=run["id"], state=run["state"])
        trace = await call("trace", run_id=run["id"])
        result["schedule_changes"] = trace["schedule_changes"]
        assert run["state"] == "completed", "Synthetic execution did not complete"
        assert info["task"]["spec"]["trigger"] == {"kind": "interval", "seconds": 86400}
        assert len(trace["schedule_changes"]) == 1, "Expected one committed schedule change"
        assert info["task"]["next_at"] > time.time() + 86000, "Next occurrence must remain in the future"
        assert run["data"]["spec"]["trigger"] == {"kind": "interval", "seconds": 1800}
        after = await client.status()
        assert after["session_id"] == status["session_id"] and after["mode"] == "standby"
        result["passed"] = True
    finally:
        if definition is not None:
            try:
                await call("archive", task_id=definition["id"])
                result["synthetic_plan_archived"] = True
            except Exception:
                result["synthetic_plan_archived"] = False
        directory = config.state_dir / "acceptance/task-self-schedule"
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = directory / (time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + ".json")
        path.write_text(json.dumps(result, ensure_ascii=False, indent=2))
        path.chmod(0o600)
        print(
            json.dumps(
                {
                    "passed": result["passed"],
                    "evidence": str(path),
                    "synthetic_plan_archived": result.get("synthetic_plan_archived"),
                },
                ensure_ascii=False,
            )
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config/config.toml")
    args = parser.parse_args()
    asyncio.run(verify(Config.load(Path(args.config))))
