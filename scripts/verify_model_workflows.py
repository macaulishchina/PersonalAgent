"""Explicit synthetic mixed-provider/MCP/lifecycle acceptance; production stays untouched.

Run with a prepared isolated gateway and prior data/budget approval. Uses real
Codex processes, task scheduler, model plugin, MCP stdio and local IPC. Voice cases
use a muted browser audio sink, never a physical microphone or speaker.
"""

import argparse
import asyncio
import copy
import json
import sqlite3
import shlex
import sys
import tempfile
import time
import uuid

from assistant_runtime.config import Config
from assistant_runtime.kernel.contracts import TaskRequest
from assistant_runtime.kernel.models import ModelRequest
from assistant_runtime.mcp.client import LocalRuntimeClient
from assistant_runtime.plugins.models.settings import ModelSettings, reasoning_effort
from scripts.model_acceptance_host import AcceptanceHost
from scripts.prepare_model_gateway import private_write
from scripts.model_voice_check import voice, voice_active
from scripts.model_workflow_checks import (
    clarification,
    upgrade,
    memory,
    search,
    performance,
    context,
    gateway_outage,
)


async def wait_for(predicate, timeout=180):
    async with asyncio.timeout(timeout):
        while True:
            value = predicate()
            if value:
                return value
            await asyncio.sleep(0.2)


def finished(host, task_id):
    runs = host.tasks.repo.runs(task_id, limit=1)
    return runs[0] if runs and runs[0]["state"] in {"completed", "failed", "unknown", "canceled"} else None


async def create(client, model, title, prompt, **spec):
    catalog = await client.invoke(TaskRequest("models", {}))
    selected = next(row for row in catalog["models"] if row["model"] == model)
    return await client.invoke(
        TaskRequest(
            "create",
            {
                "request_id": str(uuid.uuid4()),
                "spec": {
                    "title": title,
                    "prompt": prompt,
                    "notify_completion": False,
                    "auto_archive": False,
                    **spec,
                    "execution": {
                        "model": model,
                        "reasoning_effort": selected["defaultReasoningEffort"],
                        "reason": "Explicit synthetic compatibility acceptance",
                    },
                },
            },
        )
    )


async def mixed(
    host,
    output,
    *,
    child_model="go-anthropic",
    auxiliary="go-chat",
    sibling_auxiliary="go-anthropic",
    name="mixed",
):
    request_id, inference_id = str(uuid.uuid4()), str(uuid.uuid4())
    child_prompt = (
        "Synthetic coding test. Use ONLY models MCP infer_with_model, exactly twice with identical arguments: "
        + json.dumps(
            {
                "request_id": inference_id,
                "model": auxiliary,
                "text": "Synthetic coding check: evaluate Python 17 + 25. Reply only with the integer.",
            }
        )
        + ". Verify both replies have state completed and text 42, then report 42. Do not use shell or other tools."
    )
    arguments = {
        "request_id": request_id,
        "title": "Synthetic mixed-provider child",
        "prompt": child_prompt,
        "execution": {
            "model": child_model,
            "reasoning_effort": reasoning_effort(
                ModelSettings.parse(host.config.model_api).entry(child_model)
            ),
            "reason": "Explicit synthetic mixed-provider acceptance",
        },
        "notify_completion": False,
        "auto_archive": False,
    }
    foreground = await host.foreground(
        "You are authorized to create exactly one synthetic coding task. Call tasks MCP create_task with "
        + json.dumps(arguments)
        + ". Return the task ID and finish; do not poll or execute the task yourself."
    )
    private_write(output / (name + "-foreground.json"), json.dumps(foreground, ensure_ascii=False, indent=2))
    row = host.store.db.execute("SELECT id FROM job_definitions WHERE request_id=?", (request_id,)).fetchone()
    if not row:
        raise RuntimeError("Subscription foreground did not create the specified MCP task")
    client = LocalRuntimeClient(host.config.socket)
    sibling_id = str(uuid.uuid4())
    sibling = await create(
        client,
        "go-responses",
        "Synthetic parallel API child",
        "Synthetic coding check. Only use models MCP infer_with_model once with "
        + json.dumps(
            {
                "request_id": sibling_id,
                "model": sibling_auxiliary,
                "text": "Synthetic coding check: evaluate Python 6 * 7. Reply only with the integer.",
            }
        )
        + ". Report the returned number, then stop. Do not use other tools.",
    )
    first, second = await asyncio.gather(
        wait_for(lambda: finished(host, row[0])), wait_for(lambda: finished(host, sibling["id"]))
    )
    evidence = []
    for run, expected_id, expected_model in (
        (first, inference_id, auxiliary),
        (second, sibling_id, sibling_auxiliary),
    ):
        if run["state"] != "completed":
            raise RuntimeError("Mixed-provider task did not complete: " + run["state"])
        receipts = host.store.db.execute(
            "SELECT caller,model,state,result FROM model_calls WHERE id=?", (run["id"] + ":" + expected_id,)
        ).fetchall()
        if len(receipts) != 1 or receipts[0][1:3] != (expected_model, "completed"):
            raise RuntimeError("Missing unique task-bound inference receipt")
        result = json.loads(receipts[0][3])
        if result["text"].strip() != "42" or not result["caller_version"].startswith(run["id"] + ":"):
            raise RuntimeError("Mixed result or ownership mismatch")
        trace = await client.invoke(TaskRequest("trace", {"run_id": run["id"]}))
        calls = [item for item in trace["items"] if item["phase"] == "completed"]
        expected_count = 2 if run["id"] == first["id"] else 1
        if len(calls) != expected_count or any(
            item.get("server") != "models"
            or item.get("tool") != "infer_with_model"
            or item.get("status") != "completed"
            for item in calls
        ):
            raise RuntimeError("Expected MCP calls missing or unexpected tool used")
        evidence.append({"run": run, "inference": result, "trace": trace})
        try:
            await client.invoke(
                ModelRequest(
                    "infer",
                    {
                        "request_id": expected_id,
                        "model": expected_model,
                        "text": "Synthetic coding check: stale run must not execute",
                    },
                    run["id"],
                )
            )
        except RuntimeError:
            pass
        else:
            raise RuntimeError("Completed task retained inference authority")
    private_write(
        output / (name + ".json"),
        json.dumps({"foreground": foreground, "children": evidence}, ensure_ascii=False, indent=2),
    )
    return {
        "passed": True,
        "subscription_thread": foreground["thread_id"],
        "runs": [first["id"], second["id"]],
        "scope": "subscription→task MCP→API execution→models MCP→different API; durable ownership and dedup",
    }


async def gemini_mixed(host, output):
    return await mixed(
        host,
        output,
        child_model="go-gemini",
        auxiliary="go-anthropic",
        sibling_auxiliary="go-gemini",
        name="gemini-mixed",
    )


async def lifecycle(host, output):
    client = LocalRuntimeClient(host.config.socket)
    receipts = []
    for action in ("pause_run", "cancel_run"):
        marker = host.root / (action + "-started.txt")
        late = host.root / (action + "-late.txt")
        code = (
            "from pathlib import Path; import time; "
            f"p=Path({str(marker)!r}); p.write_text(p.read_text()+'start\\n' if p.exists() else 'start\\n'); "
            f"time.sleep(15); Path({str(late)!r}).write_text('late')"
        )
        command = shlex.join([sys.executable, "-c", code])
        task = await create(
            client,
            "go-responses",
            "Synthetic " + action,
            "Synthetic cancellation check. Run exactly this shell command once with exec_command "
            "and yield_time_ms=1000; wait for its completion before finishing. Do not run any other commands: "
            + command,
        )
        await wait_for(marker.exists)
        run = host.tasks.repo.runs(task["id"], limit=1)[0]
        thread = run["data"]["thread_id"]
        stopped = await client.invoke(TaskRequest(action, {"run_id": run["id"]}))
        if stopped["state"] != ("paused" if action == "pause_run" else "canceled"):
            raise RuntimeError("Stop state was not acknowledged")
        await asyncio.sleep(17)
        if late.exists() or marker.read_text() != "start\n":
            raise RuntimeError("Canceled command continued or was replayed")
        receipt = {
            "action": action,
            "run_id": run["id"],
            "thread_id": thread,
            "stopped_state": stopped["state"],
            "late_action_absent": True,
        }
        if action == "pause_run":
            await host.restart()
            client = LocalRuntimeClient(host.config.socket)
            restored = host.tasks.repo.run(run["id"])
            if restored["state"] != "paused" or restored["data"]["thread_id"] != thread:
                raise RuntimeError("Restart changed the paused execution")
            if len(host.tasks.repo.runs(task["id"], limit=10)) != 1:
                raise RuntimeError("Restart duplicated execution")
            resumed_marker = host.root / "resumed.txt"
            await client.invoke(
                TaskRequest(
                    "continue",
                    {
                        "run_id": run["id"],
                        "text": "Explicit continuation of the same synthetic test. The earlier sleeping command was canceled. "
                        "Do not run it again. Read " + str(marker) + "; verify exactly one line 'start'. "
                        "Write 'resumed' to "
                        + str(resumed_marker)
                        + " using apply_patch and finish. No other changes.",
                    },
                )
            )
            ended = await wait_for(lambda: finished(host, task["id"]))
            if (
                ended["state"] != "completed"
                or ended["data"]["thread_id"] != thread
                or not resumed_marker.exists()
                or resumed_marker.read_text().strip() != "resumed"
                or marker.read_text() != "start\n"
                or late.exists()
            ):
                raise RuntimeError("Explicit resume did not preserve execution identity and evidence")
            receipt.update(restart_preserved=True, resumed_same_thread=True, completed=True)
        receipts.append(receipt)
        private_write(output / "lifecycle.json", json.dumps(receipts, indent=2))
    return {"passed": True, "receipts": receipts}


async def inference_cancel(host, output):
    ident = str(uuid.uuid4())
    args = {
        "request_id": ident,
        "model": "go-chat",
        "text": "Synthetic coding test: explain Python list comprehensions in 300 words. Do not use tools.",
    }
    call = asyncio.create_task(host.runtime.invoke(ModelRequest("infer", args), timeout=90))
    try:
        await wait_for(
            lambda: host.store.db.execute(
                "SELECT 1 FROM model_calls WHERE id=? AND state='submitted'", ("foreground:" + ident,)
            ).fetchone(),
            timeout=10,
        )
        await asyncio.sleep(0.5)
        if call.done():
            raise RuntimeError("Inference completed before cancellation; case inconclusive")
        call.cancel()
        await asyncio.gather(call, return_exceptions=True)
        receipt = await host.runtime.invoke(ModelRequest("infer", args))
        if receipt["state"] != "unknown":
            raise RuntimeError("Canceled inference lost its unknown outcome")
        await host.restart()
        again = await host.runtime.invoke(ModelRequest("infer", args))
        if again["state"] != "unknown":
            raise RuntimeError("Restart replayed canceled inference")
        count = host.store.db.execute(
            "SELECT COUNT(*) FROM model_calls WHERE id=?", ("foreground:" + ident,)
        ).fetchone()[0]
        if count != 1:
            raise RuntimeError("Duplicate inference receipt")
        result = {
            "passed": True,
            "request_id": ident,
            "state": "unknown",
            "receipt_count": count,
            "restart_preserved": True,
        }
        private_write(output / "inference-cancel.json", json.dumps(result, indent=2))
        return result
    finally:
        if not call.done():
            call.cancel()
            await asyncio.gather(call, return_exceptions=True)


async def executor_loss(host, output):
    client = LocalRuntimeClient(host.config.socket)
    task = await create(
        client,
        "go-responses",
        "Synthetic executor loss",
        "Synthetic coding test. Do not call any tools. Explain merge sort in 800 words.",
    )

    def active():
        runs = host.tasks.repo.runs(task["id"], limit=1)
        if runs and runs[0]["state"] == "running" and runs[0]["data"].get("turn_id"):
            return runs[0]
        return None

    run = await wait_for(active)
    api = host.tasks.executor.api
    api.client.process.kill()
    await api.client.process.wait()
    lost = await wait_for(lambda: finished(host, task["id"]))
    if lost["state"] != "unknown":
        raise RuntimeError("Executor disconnect did not preserve unknown outcome")
    await host.restart()
    restored = host.tasks.repo.run(run["id"])
    if restored["state"] != "unknown" or len(host.tasks.repo.runs(task["id"], limit=10)) != 1:
        raise RuntimeError("Restart replayed uncertain execution")
    client = LocalRuntimeClient(host.config.socket)
    await client.invoke(
        TaskRequest(
            "continue",
            {
                "run_id": run["id"],
                "text": "Explicitly resume this synthetic test. Do not use tools or repeat the earlier essay. Reply only recovered.",
            },
        )
    )
    ended = await wait_for(lambda: finished(host, task["id"]))
    if (
        ended["state"] != "completed"
        or ended["data"]["thread_id"] != run["data"]["thread_id"]
        or "recovered" not in ended["data"].get("progress", "").lower()
    ):
        raise RuntimeError("Explicit recovery failed")
    result = {
        "passed": True,
        "run_id": run["id"],
        "thread_id": run["data"]["thread_id"],
        "disconnect_state": "unknown",
        "restart_preserved": True,
        "explicit_recovery": True,
    }
    private_write(output / "executor-loss.json", json.dumps(result, indent=2))
    return result


async def goal_skill(host, output):
    skill = host.root / ".agents/skills/synthetic-proof/SKILL.md"
    skill.parent.mkdir(parents=True, mode=0o700)
    proof = host.root / "skill-proof.txt"
    private_write(
        skill,
        "---\nname: synthetic-proof\ndescription: Produce the synthetic acceptance proof when explicitly requested.\n---\n"
        "Use apply_patch to create " + str(proof) + " containing exactly SYNTHETIC-7331. "
        "Read the file back. Do not change any other file. This is synthetic test data.\n",
    )
    catalog = await host.client.request("skills/list", {"cwds": [host.config.workspace], "forceReload": True})
    found = [
        item
        for row in catalog.get("data", [])
        for item in row.get("skills", [])
        if item.get("name") == "synthetic-proof" and item.get("enabled", True)
    ]
    if not found:
        raise RuntimeError("Synthetic Skill was not discovered; no model task started")
    task = await create(
        LocalRuntimeClient(host.config.socket),
        "go-anthropic",
        "Synthetic Goal and Skill",
        "Use $synthetic-proof at " + str(skill) + ". Read only that file and its requested proof. "
        "Do not search outside " + str(host.root) + ", do not read home directories or other projects. "
        "Produce its requested proof, read it back and verify it. "
        "Then mark the already active Goal complete using update_goal and report done. "
        "Use no network or MCP. This is an explicitly authorized long-term test goal.",
        long_term=True,
    )
    ended = await wait_for(lambda: finished(host, task["id"]))
    if ended["state"] != "completed" or ended["data"].get("goal", {}).get("status") != "complete":
        raise RuntimeError("Native Goal did not reach verified completion")
    if not proof.exists() or proof.read_text().strip() != "SYNTHETIC-7331":
        raise RuntimeError("Skill proof is missing")
    result = {"passed": True, "run_id": ended["id"], "goal": ended["data"].get("goal"), "skill_proof": True}
    private_write(output / "goal-skill.json", json.dumps(result, indent=2))
    return result


async def run(config, cases):
    output = config.state_dir / "acceptance/workflows" / str(uuid.uuid4())
    output.mkdir(mode=0o700, parents=True)
    # Test fixture only: permit probes to exercise the execution gate before
    # claiming production acceptance. Never write these flags to instance config.
    config.model_api = copy.deepcopy(config.model_api)
    for alias in ("go-responses", "go-anthropic", *(("go-gemini",) if "gemini-mixed" in cases else ())):
        config.model_api["entries"][alias]["execution_validated"] = True
    if "memory" in cases:
        config.model_api["memory"] = "go-anthropic"
        config.model_api["entries"]["go-anthropic"]["structured"] = True
    results = {"synthetic_only": True, "production_changed": False, "cases": {}}
    with tempfile.TemporaryDirectory(prefix="pa-api-") as directory:
        host = AcceptanceHost(config, directory, search="search" in cases)
        if "voice" in cases:
            from assistant_runtime.plugins.notifications.channels import VoiceChannel

            host.notifications.settings.voice_enabled = True
            host.notifications.settings.voice_spacing_seconds = 0
            host.notifications.register_channel("voice", VoiceChannel(host, host.notifications.settings))
        try:
            await host.start()
            for case in cases:
                started = time.monotonic()
                print("Starting", case, flush=True)
                try:
                    result = await {
                        "mixed": mixed,
                        "gemini-mixed": gemini_mixed,
                        "lifecycle": lifecycle,
                        "inference-cancel": inference_cancel,
                        "executor-loss": executor_loss,
                        "goal-skill": goal_skill,
                        "clarification": clarification,
                        "upgrade": upgrade,
                        "memory": memory,
                        "search": search,
                        "voice": voice,
                        "voice-active": voice_active,
                        "performance": performance,
                        "context": context,
                        "gateway-outage": gateway_outage,
                    }[case](host, output)
                    results["cases"][case] = {
                        **result,
                        "elapsed_ms": round((time.monotonic() - started) * 1000),
                    }
                    print(case, "passed", flush=True)
                except Exception as exc:
                    results["cases"][case] = {
                        "passed": False,
                        "error_type": type(exc).__name__,
                        "detail": str(exc)[:240],
                    }
                    print(case, "failed", type(exc).__name__, flush=True)
                private_write(output / "result.json", json.dumps(results, ensure_ascii=False, indent=2))
        finally:
            private_write(
                output / "api-events.json", json.dumps(host.api_events, ensure_ascii=False, indent=2)
            )
            with sqlite3.connect(output / "receipts.sqlite3") as backup:
                host.store.db.backup(backup)
            await host.close()
    print(output, flush=True)
    return all(row["passed"] for row in results["cases"].values())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--confirm-paid", action="store_true", required=True)
    parser.add_argument(
        "--case",
        choices=[
            "mixed",
            "gemini-mixed",
            "lifecycle",
            "inference-cancel",
            "executor-loss",
            "goal-skill",
            "clarification",
            "upgrade",
            "memory",
            "search",
            "voice",
            "voice-active",
            "performance",
            "context",
            "gateway-outage",
        ],
        action="append",
    )
    args = parser.parse_args()
    ok = asyncio.run(run(Config.load(args.config), args.case or ["mixed"]))
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
