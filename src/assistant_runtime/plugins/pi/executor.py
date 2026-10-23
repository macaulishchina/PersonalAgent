"""Task executor with verified, checkpointed model changes."""

import json
from .jobs import PiJobs


class TaskExecutor(PiJobs):
    async def launch(self, run, directory):
        upgrades = run["data"].get("model_upgrades", [])
        changed = bool(upgrades and upgrades[-1]["phase"] == "dispatching")
        if run["data"].get("execution_session_id") and changed:
            old = self
            await old.upgrade_ready(run)
            checkpoint = upgrades[-1]["request"]["checkpoint"]
            old_thread = run["data"]["execution_session_id"]
            await old.finalize({**run, "state": "completed"})
            run = {
                **run,
                "data": {
                    **run["data"],
                    "execution_session_id": None,
                    "turn_id": None,
                    "continuation": "继续原授权任务。以下是已核对的交接检查点，保留操作标识，不重复执行已完成动作；未知结果先查询：\n"
                    + json.dumps(checkpoint, ensure_ascii=False),
                },
            }
            if (
                self.notify(
                    run["id"],
                    None,
                    execution_session_id=None,
                    turn_id=None,
                    continuation=run["data"]["continuation"],
                    previous_execution_session_id=old_thread,
                )
                is False
            ):
                return
        await super().launch(run, directory)
