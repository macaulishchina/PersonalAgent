"""Explicit installed Jev transport acceptance: three paid synthetic calls.

No device actions, conversation input or periodic keepwarm. Run after deployment.
Run soon after restart to observe the effect of the one startup metadata GET.
"""

import argparse
import asyncio
import json
from pathlib import Path
import time
import uuid

from assistant_runtime.config import Config
from assistant_runtime.kernel.contracts import DecisionRequest, DecisionQuery
from assistant_runtime.mcp.client import LocalRuntimeClient
from assistant_runtime.plugins.decisions.settings import DecisionSettings


async def verify(config):
    settings = DecisionSettings.parse(config.decisions)
    if not settings.enabled or not settings.external_data_consent or not settings.warmup_enabled:
        raise RuntimeError("Enabled, consented startup warmup is required")
    client = LocalRuntimeClient(config.socket, timeout=5)
    installed = await client.invoke(DecisionRequest("status", {}))
    if (
        installed["active_timeout_seconds"] != 3
        or not installed["active_enabled"]
        or not installed["credential_available"]
        or installed["queue_depth"]
        or installed["retry_in_seconds"]
        or installed.get("transport", {}).get("warmup", {}).get("state") not in {"completed", "failed"}
    ):
        raise RuntimeError("Deploy and verify the installed 3-second deadline/startup warmup first")
    receipt = {
        "synthetic": True,
        "started_at": time.time(),
        "installed": {
            "active_timeout_seconds": installed["active_timeout_seconds"],
            "transport": installed["transport"],
        },
        "judgments": [],
        "result": "unconfirmed",
    }
    run = uuid.uuid4().hex
    try:
        for name, idle in (("after_warmup", 0), ("immediate_reuse", 0), ("after_20s_idle", 20)):
            if idle:
                await asyncio.sleep(idle)
            row = {"name": name, "idle_seconds": idle}
            receipt["judgments"].append(row)
            started = time.monotonic()
            try:
                result = await client.invoke(
                    DecisionQuery(
                        "selection",
                        f"synthetic-transport:{run}:{name}",
                        {
                            "question": "当前目标是读取已知设备温度，选择下一步。",
                            "context": {"summary": "已有读取工具和字段，读回即可核验。"},
                            "options": {"read": "直接调用现有只读工具", "research": "开展复杂理论研究"},
                        },
                    )
                )
                row.update(result=result, elapsed_ms=round((time.monotonic() - started) * 1000))
                status = await client.invoke(DecisionRequest("status", {}))
                row["transport"] = status["transport"]["last_judgment"]
                if not result.get("receipt_id") or result["receipt_id"] != row["transport"]["receipt_id"]:
                    raise RuntimeError("No matching transport receipt; inspect before any retry")
                committed = await client.invoke(DecisionRequest("get", {"id": result["receipt_id"]}))
                row.update(state=committed["state"], latency_ms=committed.get("latency_ms"))
                if row["state"] != "completed":
                    raise RuntimeError("Synthetic judgment did not complete; inspect before any retry")
            finally:
                row.setdefault("elapsed_ms", round((time.monotonic() - started) * 1000))
        receipt["reused_after_idle"] = receipt["judgments"][-1]["transport"]["reused_connection"]
        receipt["result"] = "completed"
    finally:
        directory = config.state_dir / "acceptance/decisions"
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = directory / ("transport-" + time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + ".json")
        path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2))
        path.chmod(0o600)
        print(path)
        print(json.dumps(receipt, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config/config.toml")
    args = parser.parse_args()
    asyncio.run(verify(Config.load(Path(args.config))))
