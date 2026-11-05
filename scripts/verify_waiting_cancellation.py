"""Explicit paid-provider acceptance of targeted task cancellation during clarification.

Uses fresh private state and existing authorized API credentials in memory. Never
changes the source instance, replays an uncertain operation, or overwrites evidence.
"""

import argparse
import asyncio
from copy import deepcopy
import json
import os
from pathlib import Path
import time
from unittest.mock import patch
import uuid

from assistant_runtime.api import ConversationRequest
from assistant_runtime.api.contracts import TaskRequest
from assistant_runtime.app.config import Config
from assistant_runtime.app.host import AssistantService
from assistant_runtime.core.models.credentials import credential
from assistant_runtime.core.models.settings import ModelSettings


async def verify(source, models, output, *, restart_waiting=False):
    original = ModelSettings.parse(source.model_api)
    selected = set([*models, source.main_model])
    for model in selected:
        original.route(model)
    registry = deepcopy(source.model_api)
    registry["entries"] = {model: registry["entries"][model] for model in selected}
    providers = {entry["provider"] for entry in registry["entries"].values()}
    registry["providers"] = {name: registry["providers"][name] for name in providers}
    if any(provider["protocol"] == "pi" for provider in registry["providers"].values()):
        raise ValueError("This acceptance requires configured API routes")
    registry.update(
        accounts={},
        memory="",
        retry={"max_retries": 0, "unknown_retries": 0, "max_delay_seconds": 1},
        execution_max_output_tokens=1024,
        timeout_seconds=90,
    )
    for entry in registry["entries"].values():
        entry["execution_max_output_tokens"] = 1024
    secrets = {
        original.providers[name]["key_env"]: credential(source.root, original.providers[name]["key_env"])
        for name in providers
    }
    root = output.resolve() / "instance"
    root.mkdir()
    config = Config(
        root=root,
        workspace=str(root),
        plugins=[
            "ai_usage",
            "models",
            "workfiles",
            "pi",
            "execution",
            "conversation",
            "notifications",
            "tasks",
        ],
        main_model=source.main_model,
        main_reasoning_effort="auto",
        model_api=registry,
        ai_usage={"max_requests": 3 if restart_waiting else 8},
        execution={"concurrency": 4},
        tasks={"concurrency": 2, "model_policy": {"review_mode": "off"}},
    )
    with patch.dict(os.environ, secrets):
        host = AssistantService(config)
        running = asyncio.create_task(host.run())
        try:
            await asyncio.wait_for(host.runtime.ready.wait(), 30)
            invoke = host.runtime.invoke

            async def wait(task, states):
                async with asyncio.timeout(120):
                    while True:
                        saved = await invoke(TaskRequest("get", {"task_id": task["id"]}))
                        if saved["runs"]:
                            run = saved["runs"][0]
                            if run["state"] in states:
                                return run
                            if run["state"] in {"failed", "unknown", "canceled", "completed"}:
                                raise RuntimeError("Unexpected task outcome: " + run["state"])
                        await asyncio.sleep(0.05)

            tasks = []
            for model in models:
                tasks.append(
                    await invoke(
                        TaskRequest(
                            "create",
                            {
                                "request_id": str(uuid.uuid4()),
                                "spec": {
                                    "title": "Synthetic clarification cancellation acceptance",
                                    "prompt": "This is synthetic read-only acceptance. First call request_user_input to ask me to confirm the material, then wait. Do not assume confirmation. After an explicit answer, reply exactly PA_WAIT_ANSWERED. Do not call other tools.",
                                    "execution": {"model": model, "effort_mode": "model_default"},
                                    "tools": ["request_user_input"],
                                    "notify_completion": False,
                                    "notify_voice": False,
                                    "auto_archive": False,
                                },
                            },
                        )
                    )
                )
            runs = [await wait(task, {"waiting_user"}) for task in tasks]
            restored_waiting = None
            if restart_waiting:
                previous = runs
                before = [
                    await host.ai_usage.service.snapshot(run["data"]["ai_usage"]["work_id"]) for run in runs
                ]
                host.stop_event.set()
                await asyncio.wait_for(running, 30)
                host = AssistantService(config)
                running = asyncio.create_task(host.run())
                await asyncio.wait_for(host.runtime.ready.wait(), 30)
                invoke = host.runtime.invoke
                runs = [await wait(task, {"waiting_user"}) for task in tasks]
                after = [
                    await host.ai_usage.service.snapshot(run["data"]["ai_usage"]["work_id"]) for run in runs
                ]
                for old, new, usage_before, usage_after in zip(previous, runs, before, after):
                    assert old["id"] == new["id"]
                    for key in ("turn_id", "execution_id", "question_id"):
                        assert old["data"][key] == new["data"][key]
                    assert new["data"]["recovery"]["restored"]
                    assert usage_before["requests"] == usage_after["requests"]
                restored_waiting = {
                    "model_calls": 0,
                    "identities_preserved": True,
                    "questions": [run["data"]["question_id"] for run in runs],
                }
            # The ordinary foreground remains usable while both task questions wait.
            chat = await invoke(ConversationRequest("create", {"title": "Synthetic independent foreground"}))
            request = str(uuid.uuid4())
            await invoke(
                ConversationRequest(
                    "submit",
                    {
                        "session_id": chat["id"],
                        "request_id": request,
                        "text": "Synthetic acceptance: reply exactly PA_FOREGROUND_OK. Do not use tools or answer background questions.",
                    },
                )
            )
            async with asyncio.timeout(120):
                while True:
                    receipt = await invoke(
                        ConversationRequest("receipt", {"session_id": chat["id"], "request_id": request})
                    )
                    if receipt["state"] == "completed":
                        break
                    if receipt["state"] in {"failed", "unknown"}:
                        raise RuntimeError("Foreground failed")
                    await asyncio.sleep(0.05)
            await wait(tasks[0], {"waiting_user"})
            await wait(tasks[1], {"waiting_user"})
            answer_args = {"run_id": runs[1]["id"], "text": "Confirmed synthetic material. Continue."}
            if restart_waiting:
                answer_args.update(question_id=runs[1]["data"]["question_id"], message_id=str(uuid.uuid4()))
            await invoke(TaskRequest("supplement", answer_args))
            answered = await wait(tasks[1], {"completed"})
            if "PA_WAIT_ANSWERED" not in answered["data"].get("result", ""):
                raise RuntimeError("Answered task returned unexpected result")
            started = time.monotonic()
            stopped = await invoke(TaskRequest("cancel_run", {"run_id": runs[0]["id"]}))
            elapsed = time.monotonic() - started
            assert stopped["state"] == "canceled" and not stopped["data"]["questions"]
            repeated = await invoke(TaskRequest("cancel_run", {"run_id": runs[0]["id"]}))
            assert repeated["state"] == "canceled"
            service = host.backend.executions
            execution = stopped["data"]["execution_id"]
            assert service.records.get(execution)["state"] == "cancelled"
            assert not service.task_evidence(stopped["id"])[0]["unresolved_operations"]
            assert execution not in service.questions and execution not in service.admission.leases
            assert not host.backend.client.ui_requests
            summary = {
                "state": "passed",
                "models": models,
                "foreground_model": source.main_model,
                "run_ids": [run["id"] for run in runs],
                "stop_seconds": round(elapsed, 3),
                "canceled": stopped["state"],
                "answered": answered["state"],
                "foreground": receipt["state"],
                "questions_and_capacity_released": True,
                "repeat_cancel": repeated["state"],
                "waiting_restart": restored_waiting,
            }
            if restart_waiting:
                summary["usage"] = await host.ai_usage.service.call("query", {"group_by": "model"})
                summary["limits"] = {
                    "requests_per_work": 3,
                    "output_tokens_per_request": 1024,
                    "timeout_seconds": 90,
                    "request_retries": 0,
                }
        finally:
            host.stop_event.set()
            await asyncio.wait_for(running, 30)
        # Read the same receipts after a clean restart; never submit them again.
        host = AssistantService(config)
        running = asyncio.create_task(host.run())
        try:
            await asyncio.wait_for(host.runtime.ready.wait(), 30)
            for task, expected in zip(tasks, ("canceled", "completed")):
                saved = await host.runtime.invoke(TaskRequest("get", {"task_id": task["id"]}))
                assert saved["runs"][0]["state"] == expected
            summary["restart_preserved"] = True
        finally:
            host.stop_event.set()
            await asyncio.wait_for(running, 30)
        return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--models", required=True, nargs=2)
    parser.add_argument("--output", required=True, type=Path, help="New evidence directory; must not exist")
    parser.add_argument("--confirm-paid", action="store_true")
    parser.add_argument(
        "--restart-waiting",
        action="store_true",
        help="Restore both pending questions before answering/canceling; max 3 requests per work",
    )
    args = parser.parse_args()
    if not args.confirm_paid:
        parser.error("Real supplier calls require --confirm-paid")
    args.output.mkdir(parents=True, exist_ok=False)
    try:
        result = asyncio.run(
            verify(Config.load(args.config), args.models, args.output, restart_waiting=args.restart_waiting)
        )
    except Exception as exc:
        result = {"state": "failed", "error": type(exc).__name__}
    (args.output / "summary.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result))
    raise SystemExit(0 if result["state"] == "passed" else 1)


if __name__ == "__main__":
    main()
