"""Owned subprocess for abrupt-host-exit acceptance; synthetic config only."""

import asyncio
import json
from pathlib import Path
import sys
import uuid

from assistant_runtime.api.contracts import TaskRequest
from assistant_runtime.app.config import Config
from assistant_runtime.app.host import AssistantService


async def main():
    values = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    values["root"] = Path(values["root"])
    host = AssistantService(Config(**values))
    running = asyncio.create_task(host.run())
    await asyncio.wait_for(host.runtime.ready.wait(), 30)
    tasks = []
    for model in ("model-a", "model-b"):
        task = await host.runtime.invoke(
            TaskRequest(
                "create",
                {
                    "request_id": str(uuid.uuid4()),
                    "spec": {
                        "title": "Synthetic abrupt-restart task",
                        "prompt": "Ask and wait",
                        "execution": {"model": model, "effort_mode": "model_default"},
                        "tools": ["request_user_input"],
                        "notify_completion": False,
                        "notify_voice": False,
                        "auto_archive": False,
                    },
                },
            )
        )
        async with asyncio.timeout(25):
            while True:
                saved = await host.runtime.invoke(TaskRequest("get", {"task_id": task["id"]}))
                if saved["runs"] and saved["runs"][0]["state"] == "waiting_user":
                    break
                await asyncio.sleep(0.02)
        tasks.append({"task": task, "run": saved["runs"][0]})
    print("WAITING_TEST:" + json.dumps(tasks), flush=True)
    await running


if __name__ == "__main__":
    asyncio.run(main())
