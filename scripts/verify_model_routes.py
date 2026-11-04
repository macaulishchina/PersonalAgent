"""Explicit real-provider acceptance of public route/default/delegation contracts.

Uses synthetic prompts and an isolated instance. Reads configured API credentials in
memory; never stops, upgrades or changes the source instance. No automatic retries.
"""

import argparse
import asyncio
from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import time
from unittest.mock import patch
import uuid

from assistant_runtime.api import ConversationRequest
from assistant_runtime.api.contracts import TaskRequest
from assistant_runtime.api.models import ModelRequest
from assistant_runtime.app.config import Config
from assistant_runtime.app.host import AssistantService
from assistant_runtime.core.models.credentials import credential
from assistant_runtime.core.models.settings import ModelSettings


async def verify(source, models):
    original = ModelSettings.parse(source.model_api)
    for model in models:
        original.require_route({k: original.route(model)[k] for k in ("model", "revision")})
        if original.providers[original.entries[model]["provider"]]["protocol"] == "pi":
            raise ValueError("This acceptance uses API connections; native account acceptance is separate")
    registry = deepcopy(source.model_api)
    registry["entries"] = {model: registry["entries"][model] for model in models}
    providers = {entry["provider"] for entry in registry["entries"].values()}
    registry["providers"] = {name: registry["providers"][name] for name in providers}
    registry["accounts"] = {}
    registry["memory"] = ""
    registry["retry"] = {"max_retries": 0, "unknown_retries": 0, "max_delay_seconds": 1}
    registry["execution_max_output_tokens"] = 1024
    registry["timeout_seconds"] = 90
    for entry in registry["entries"].values():
        entry["execution_max_output_tokens"] = 1024
    secrets = {
        original.providers[name]["key_env"]: credential(source.root, original.providers[name]["key_env"])
        for name in providers
    }
    with (
        tempfile.TemporaryDirectory(prefix="pa-route-acceptance-") as folder,
        patch.dict(os.environ, secrets),
    ):
        root = Path(folder)
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
            main_model=models[0],
            main_reasoning_effort="auto",
            model_api=registry,
            ai_usage={"max_requests": 4},
            tasks={"model_policy": {"review_mode": "off"}},
        )
        host = AssistantService(config)
        running = asyncio.create_task(host.run())
        started = time.monotonic()
        try:
            await asyncio.wait_for(host.runtime.ready.wait(), 30)
            invoke = host.runtime.invoke
            catalog = await invoke(ModelRequest("catalog"))
            routes = {row["model"]: row["route"] for row in catalog["models"]}
            conditions = {model: {k: routes[model][k] for k in ("model", "revision")} for model in models}

            async def completed(task_id):
                async with asyncio.timeout(150):
                    while True:
                        result = await invoke(TaskRequest("get", {"task_id": task_id}))
                        runs = result["runs"]
                        if runs and runs[0]["state"] in {"completed", "failed", "unknown", "canceled"}:
                            if runs[0]["state"] != "completed":
                                raise RuntimeError("Synthetic task did not complete: " + runs[0]["state"])
                            return runs[0]
                        await asyncio.sleep(0.1)

            direct_id = str(uuid.uuid4())
            direct_args = {
                "request_id": direct_id,
                "spec": {
                    "title": "Synthetic direct default",
                    "prompt": "Reply exactly PA_ROUTE_OK. Use no tools.",
                    "execution": {
                        "model": models[1],
                        "expected_model": conditions[models[1]],
                        "effort_mode": "model_default",
                    },
                    "tools": [],
                    "notify_completion": False,
                    "notify_voice": False,
                    "auto_archive": False,
                },
            }
            direct = await invoke(TaskRequest("create", direct_args))
            receipt = await invoke(TaskRequest("receipt", direct_args))
            assert receipt["matches"] and receipt["task_id"] == direct["id"]
            assert (await invoke(TaskRequest("create", direct_args)))["id"] == direct["id"]
            direct_run = await completed(direct["id"])
            target = original.entries[models[1]]
            effort = next(
                (e for e in target["reasoning_efforts"] if e in {"low", "none", "minimal"}),
                target["reasoning_efforts"][0] if target["reasoning_efforts"] else "auto",
            )
            delegated_id = str(uuid.uuid4())
            execution = {
                "model": models[1],
                "expected_model": conditions[models[1]],
                "reasoning_effort": effort,
                "model_source": "user",
                "effort_source": "user",
                "reason": "Synthetic explicit delegation acceptance",
            }
            chat = await invoke(
                ConversationRequest(
                    "create",
                    {
                        "title": "Synthetic delegation",
                        "policy": {
                            "tools": ["mcp__tasks__create_task"],
                            "capture_memory": False,
                            "instructions": "Synthetic acceptance only. Create exactly the one requested task, then return a short acknowledgment. Do not use any other tools.",
                        },
                    },
                )
            )
            prompt = (
                "Call create_task exactly once with these arguments, then acknowledge. The authorization is only this synthetic text task: "
                + json.dumps(
                    {
                        "request_id": delegated_id,
                        "title": "Synthetic delegated task",
                        "prompt": "Reply exactly PA_DELEGATED_OK. Use no tools.",
                        "execution": execution,
                        "notify_completion": False,
                        "notify_voice": False,
                        "auto_archive": False,
                        "tools": [],
                    }
                )
            )
            chat_id = str(uuid.uuid4())
            await invoke(
                ConversationRequest(
                    "submit",
                    {
                        "session_id": chat["id"],
                        "request_id": chat_id,
                        "text": prompt,
                        "expected_model": conditions[models[0]],
                    },
                )
            )
            async with asyncio.timeout(150):
                while True:
                    parent = await invoke(
                        ConversationRequest(
                            "receipt",
                            {"session_id": chat["id"], "request_id": chat_id, "include_execution": True},
                        )
                    )
                    queue = await invoke(ConversationRequest("queue", {"session_id": chat["id"]}))
                    if parent["found"] and not queue["requests"]:
                        break
                    await asyncio.sleep(0.1)
            child_receipt = await invoke(TaskRequest("receipt", {"request_id": delegated_id}))
            if not child_receipt["found"]:
                raise RuntimeError("The initiating model did not create the requested synthetic task")
            child_run = await completed(child_receipt["task_id"])
            state = await invoke(ConversationRequest("state", {"session_id": chat["id"]}))
            child_work = child_run["data"]["ai_usage"]["work_id"]
            assert child_work == state["ai_usage"]["work_id"], (
                "Delegation did not preserve the shared work identity"
            )
            for run in (direct_run, child_run):
                assert run["data"]["execution_facts"]["route"] == routes[models[1]]
                assert run["data"].get("result_ref")
                assert run["data"]["spec"]["notify_completion"] is False
            assert child_run["data"]["execution_facts"]["reasoning_effort"] == effort
            assert direct_run["data"]["execution_facts"]["reasoning_effort"] == target.get(
                "default_reasoning_effort", "auto"
            )
            assert parent["execution"]["route"] == routes[models[0]]
            return {
                "state": "passed",
                "scope": "synthetic_real_api_isolated_instance",
                "models": models,
                "routes": routes,
                "direct_default_effort": direct_run["data"]["execution_facts"]["reasoning_effort"],
                "delegated_effort": effort,
                "shared_work_verified": True,
                "lost_ack_receipt_verified": True,
                "duplicate_create_verified": True,
                "completion_notifications_disabled": True,
                "parent_execution": parent["execution"],
                "task_execution_facts": [r["data"]["execution_facts"] for r in (direct_run, child_run)],
                "elapsed_seconds": round(time.monotonic() - started, 3),
            }
        finally:
            host.stop_event.set()
            await asyncio.wait_for(running, 30)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--models", required=True, nargs=2)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--confirm-paid", action="store_true")
    args = parser.parse_args()
    if not args.confirm_paid:
        parser.error("Real supplier calls require --confirm-paid")
    try:
        result = asyncio.run(verify(Config.load(args.config), args.models))
    except Exception as exc:
        result = {"state": "failed", "error": type(exc).__name__}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))
    raise SystemExit(0 if result["state"] == "passed" else 1)


if __name__ == "__main__":
    main()
