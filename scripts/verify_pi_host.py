"""Bounded real-API delegation acceptance in an isolated host; no production/device actions."""

import argparse
import asyncio
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import uuid

from assistant_runtime.app.host import AssistantService
from assistant_runtime.app.config import Config
from assistant_runtime.core.models.gateway import gateway_credential
from assistant_runtime.core.models.settings import ModelSettings, reasoning_effort


async def verify(config, main_model, task_model, *, effort="low", max_requests=10, max_tokens=1024):
    if not 1 <= max_requests <= 12 or not 256 <= max_tokens <= 2048:
        raise ValueError("Host acceptance budget outside bounds")
    settings = ModelSettings.parse(config.model_api)
    for ident in {main_model, task_model}:
        entry = settings.entry(ident)
        if not entry.get("execution_validated"):
            raise ValueError("Host acceptance needs an accepted basic executor")
        reasoning_effort(entry, effort)
    evidence = {"scope": "isolated_synthetic_host", "state": "failed", "production_changed": False}
    with tempfile.TemporaryDirectory(prefix="pi-release-host-") as folder:
        root = Path(folder)
        raw = deepcopy(config.model_api)
        raw["memory"] = ""
        raw["entries"] = {name: raw["entries"][name] for name in {main_model, task_model}}
        owners = {entry["provider"] for entry in raw["entries"].values()}
        raw["providers"] = {name: raw["providers"][name] for name in owners}
        for entry in raw["entries"].values():
            entry.update(fallbacks=[], max_output_tokens=max_tokens, execution_max_output_tokens=max_tokens)
        key = gateway_credential(config.root, settings)
        (root / ".env").write_text(settings.gateway_key_env + "=" + key + "\n")
        (root / ".env").chmod(0o600)
        isolated = Config(
            root=root,
            workspace=folder,
            pi_binary=config.pi_binary,
            main_model=main_model,
            main_reasoning_effort=effort,
            plugins=[
                "ai_usage",
                "workfiles",
                "devices",
                "assistant",
                "conversation",
                "execution",
                "models",
                "pi",
                "voice_disabled",
                "tasks",
                "notifications",
            ],
            memory_enabled=False,
            model_api=raw,
            ai_usage={"max_requests": max_requests, "work_limits": {"max_seconds": 120}},
            tasks={
                "concurrency": 4,
                "max_goal_rounds": 1,
                "model_policy": {
                    "max_upgrades": 0,
                    "review_mode": "off",
                    "profiles": {
                        name: {"model": task_model, "reasoning_effort": effort}
                        for name in ("fast", "balanced", "deep", "ultra")
                    },
                },
            },
            notifications={"channels": ["chat"], "voice_enabled": False},
        )
        host = AssistantService(isolated)
        from assistant_runtime.core.pi.accounts import NativeAccounts

        # Share the original locked credential store; never copy rotating OAuth credentials.
        host.backend.client.accounts = NativeAccounts(config.root, ModelSettings.parse(raw))
        serving = asyncio.create_task(host.run())
        try:
            await asyncio.wait_for(host.runtime.ready.wait(), 20)
            target = root / "result.txt"
            request_id = str(uuid.uuid4())
            prompt = (
                "Synthetic release acceptance, local temporary files only. Delegate exactly ONE background "
                "task with tasks create_task, feedback_owner=conversation, execution model="
                + task_model
                + ", reasoning_effort="
                + effort
                + ", model_source=user, effort_source=user. "
                "The delegated task must write exactly SYNTHETIC_OK to "
                + str(target)
                + " and read it back; no network, device actions, personal files, nested tasks, plans or "
                "notifications. This is a one-shot task. After creating it, briefly confirm delegation. "
                "Do not do the file write yourself."
            )
            await host.conversation.submit(request_id, prompt, [])
            await host.conversation.submit(request_id, prompt, [])
            async with asyncio.timeout(120):
                while True:
                    plugin = host.runtime.plugins["tasks"]
                    definitions = plugin.repo.definitions()
                    runs = [run for d in definitions for run in plugin.repo.runs(d["id"])]
                    if runs and runs[0]["state"] in {
                        "completed",
                        "failed",
                        "unknown",
                        "waiting_user",
                        "paused",
                    }:
                        break
                    if serving.done():
                        await serving
                    recent = host.store.executions.recent()
                    if (
                        not runs
                        and recent
                        and not host.backend.busy
                        and recent[0]["status"]
                        in {
                            "completed",
                            "failed",
                            "interrupted",
                            "unknown",
                        }
                    ):
                        evidence.update(task_count=0, stage="before_delegation")
                        raise RuntimeError("foreground_ended_without_delegation")
                    await asyncio.sleep(0.2)
            evidence.update(task_count=len(definitions), task_state=runs[0]["state"])
            if runs[0]["state"] != "completed":
                raise RuntimeError("delegated_task_not_completed")
            if target.read_text().strip() != "SYNTHETIC_OK" or len(definitions) != 1:
                raise RuntimeError("readback_or_dedup_mismatch")
            async with asyncio.timeout(20):
                while host.backend.busy:
                    await asyncio.sleep(0.1)
            scope = await host.ai_usage.service.binding(runs[0]["id"])
            summary = await host.ai_usage.service.snapshot(scope.work_id)
            calls = await host.ai_usage.service.call("query", {"work_id": scope.work_id})
            if len({row["caller"] for row in calls["items"]}) != 2:
                raise RuntimeError("caller_identity_mismatch")
            evidence.update(
                state="completed",
                shared_work_id=True,
                requests=summary["requests"],
                callers=2,
                file_readback=True,
                voice_opened=host.assistant.voice.opened,
                usage=summary,
                calls=calls["items"],
                main_model=main_model,
                task_model=task_model,
            )
        except Exception as exc:
            evidence.update(state="failed", error=type(exc).__name__)
        finally:
            host.stop_event.set()
            try:
                await asyncio.wait_for(serving, 20)
            except Exception as exc:
                evidence.update(state="failed", cleanup_error=type(exc).__name__)
    return evidence


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--main-model", required=True)
    parser.add_argument("--task-model", required=True)
    parser.add_argument("--reasoning-effort", default="low")
    parser.add_argument("--max-requests", type=int, default=10)
    parser.add_argument("--max-output-tokens", type=int, default=1024)
    parser.add_argument("--confirm-paid", action="store_true")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if not args.confirm_paid:
        parser.error("Prior synthetic-data/budget authorization and --confirm-paid are required")
    result = asyncio.run(
        verify(
            Config.load(args.config),
            args.main_model,
            args.task_model,
            effort=args.reasoning_effort,
            max_requests=args.max_requests,
            max_tokens=args.max_output_tokens,
        )
    )
    args.output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    args.output.chmod(0o600)
    print(json.dumps({key: value for key, value in result.items() if key not in {"calls", "usage"}}))
    raise SystemExit(0 if result["state"] == "completed" else 1)


if __name__ == "__main__":
    main()
