"""Explicit paid-provider acceptance through the installed optional capability.

Uses synthetic cases, no household audio, device action or task execution.
Semantic agreement on this tiny fixture is not a household-accuracy measurement.
"""

import argparse
import asyncio
import json
from pathlib import Path
import time
import uuid

from fastmcp import Client

from assistant_runtime.config import Config
from assistant_runtime.kernel.contracts import DecisionRequest, DecisionQuery
from assistant_runtime.mcp.client import LocalRuntimeClient
from assistant_runtime.mcp.decisions import create_server


CASES = [
    ("exit", "voice_intent", {"utterance": "退下吧", "execution_active": False}, "sleep"),
    (
        "quoted",
        "voice_intent",
        {"utterance": "‘退下’这个词是什么意思？", "execution_active": False},
        "new_request",
    ),
    ("stop", "voice_intent", {"utterance": "停一下，别说了", "execution_active": True}, "stop"),
    ("cancel", "voice_intent", {"utterance": "不用做了，取消当前任务", "execution_active": True}, "cancel"),
    (
        "takeover",
        "voice_intent",
        {"utterance": "我自己来操作，你别动了", "execution_active": True},
        "takeover",
    ),
    (
        "correction",
        "voice_intent",
        {"utterance": "不是客厅，是卧室的电视", "execution_active": True},
        "correct",
    ),
    (
        "related",
        "event_relevance",
        {
            "objective": "关注客厅电视开启和待机状态的变化。",
            "event": {
                "type": "television.state_changed",
                "subject": "living_tv",
                "evidence": "observation",
                "data": {"before": "standby", "after": "on"},
            },
        },
        "relevant",
    ),
    (
        "unrelated",
        "event_relevance",
        {
            "objective": "仅关注客厅电视开启和待机状态的变化。",
            "event": {
                "type": "sensor.humidity_changed",
                "subject": "balcony_sensor",
                "evidence": "observation",
                "data": {"humidity": 60},
            },
        },
        "irrelevant",
    ),
    (
        "model_fast",
        "model_profile",
        {"objective": "只读查询已知设备当前温度，已有工具和字段，读回结果即可验收。"},
        "fast",
    ),
    (
        "model_deep",
        "model_profile",
        {
            "objective": "诊断分布式系统的罕见数据损坏，证据互相冲突，无现成流程；需排除多种因果假设并验证根因。"
        },
        "deep",
    ),
]


async def verify(config):
    client = LocalRuntimeClient(config.socket)

    async def call(action, **args):
        return await client.invoke(DecisionRequest(action, args))

    initial = await call("status")
    if (
        not all(initial[k] for k in ("enabled", "external_data_consent", "credential_available"))
        or initial["execution_enabled"]
    ):
        raise RuntimeError("An available, consented, effect-free judgment capability is required")
    active_count = 4 if initial["mode"] == "active" else 0
    if initial["attempts_today"] + len(CASES) + active_count > initial["daily_limit"]:
        raise RuntimeError("Synthetic acceptance would exceed the configured daily attempt budget")
    receipt = {"synthetic": True, "started_at": time.time(), "cases": [], "active": []}
    run = uuid.uuid4().hex
    try:
        for name, kind, state, expected in CASES:
            queued = await call("evaluate", kind=kind, reference=f"synthetic:{run}:{name}", state=state)
            if not queued["accepted"]:
                raise RuntimeError("Synthetic sample was not accepted")
            deadline = time.monotonic() + 45
            result = None
            while time.monotonic() < deadline:
                try:
                    result = await call("get", id=queued["id"])
                except RuntimeError:
                    result = None
                if result and result["state"] != "dispatching":
                    break
                await asyncio.sleep(0.2)
            if not result or result["state"] != "completed":
                receipt["cases"].append({"name": name, "state": result["state"] if result else "unconfirmed"})
                raise RuntimeError("Synthetic evaluation did not complete; inspect before any retry")
            await call("label", id=queued["id"], expected=expected)
            receipt["cases"].append(
                {
                    "name": name,
                    "id": queued["id"],
                    "expected": expected,
                    "matches": result["answer"]["choice"] == expected,
                    "answer": result["answer"],
                    "latency_ms": result["latency_ms"],
                }
            )
        if initial["mode"] == "active":
            # Exercise real installed query adoption policy without invoking its owners' actions.
            for index in (0, 6, 8):
                name, kind, state, expected = CASES[index]
                started = time.monotonic()
                result = await client.invoke(DecisionQuery(kind, f"synthetic-active:{run}:{name}", state))
                receipt["active"].append(
                    {
                        "name": name,
                        "result": result,
                        "matches": result["status"] == "ok" and result["choice"] == expected,
                        "elapsed_ms": round((time.monotonic() - started) * 1000),
                    }
                )
            async with Client(create_server(client)) as mcp:
                started = time.monotonic()
                result = (
                    await mcp.call_tool(
                        "judge_options",
                        {
                            "request_id": str(uuid.uuid4()),
                            "question": "依据工具读回的页面差异，应选择哪种后续处理方式？",
                            "context": {
                                "summary": "现有流程依赖的页面按钮已不在原位置。当前只读取了页面，没有执行写操作。"
                            },
                            "options": {"inspect": "先检查新界面再制定步骤", "repeat": "原样重复旧流程"},
                        },
                    )
                ).structured_content
                receipt["active"].append(
                    {
                        "name": "mcp_selection",
                        "result": result,
                        "matches": result["status"] == "ok" and result["choice"] == "inspect",
                        "elapsed_ms": round((time.monotonic() - started) * 1000),
                    }
                )
        receipt["result"] = "completed"
    finally:
        directory = config.state_dir / "acceptance/decisions"
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = directory / (time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + ".json")
        path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2))
        path.chmod(0o600)
        print(path)
    print(
        json.dumps(
            {
                "completed": len(receipt["cases"]),
                "fixture_matches": sum(c.get("matches", False) for c in receipt["cases"]),
                "latency_ms": [c["latency_ms"] for c in receipt["cases"]],
                "active_matches": sum(c["matches"] for c in receipt["active"]),
                "active_results": [
                    {
                        "name": c["name"],
                        "status": c["result"]["status"],
                        "reason": c["result"].get("reason"),
                        "elapsed_ms": c["elapsed_ms"],
                    }
                    for c in receipt["active"]
                ],
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.toml")
    args = parser.parse_args()
    asyncio.run(verify(Config.load(Path(args.config))))
