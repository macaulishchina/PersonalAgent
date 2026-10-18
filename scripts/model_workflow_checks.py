"""Additional explicit real-provider acceptance; synthetic data and private state only."""

import asyncio
import json
import uuid
import time
import resource
import hashlib
from dataclasses import replace
from pathlib import Path

from assistant_runtime.kernel.contracts import TaskRequest
from assistant_runtime.mcp.client import LocalRuntimeClient
from assistant_runtime.plugins.tasks.selection import ModelPolicy
from scripts.prepare_model_gateway import private_write


async def gateway_outage(host, output):
    from assistant_runtime.kernel.models import ModelRequest

    compose = host.source.state_dir / "model-api/compose.json"
    # This destructive fault injection is restricted to an explicit acceptance
    # instance. Never stop a normal deployment's gateway through this script.
    if "acceptance" not in host.source.root.parts or not compose.is_file():
        raise ValueError("Gateway outage requires an isolated acceptance instance")

    async def docker(*args):
        process = await asyncio.create_subprocess_exec(
            "docker",
            "compose",
            "-f",
            str(compose),
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await asyncio.wait_for(process.communicate(), 45)
        if process.returncode:
            raise RuntimeError("Isolated gateway lifecycle command failed")

    request = ModelRequest(
        "infer",
        {
            "request_id": str(uuid.uuid4()),
            "model": "go-anthropic",
            "text": "Synthetic coding failure injection; return 42.",
        },
    )
    try:
        await docker("stop")
        error = None
        try:
            await host.runtime.invoke(request, timeout=10)
        except Exception as exc:
            error = type(exc).__name__
        if not error:
            raise RuntimeError("Stopped gateway unexpectedly completed inference")
        duplicate = await host.runtime.invoke(request)
        if duplicate.get("state") != "unknown":
            raise RuntimeError("Uncertain gateway call was resubmitted or lost")
        foreground = await host.foreground("Synthetic isolation check: answer exactly 42 without tools.")
        texts = [
            e["params"]["item"].get("text", "")
            for e in foreground["events"]
            if e.get("method") == "item/completed"
            and e.get("params", {}).get("item", {}).get("type") == "agentMessage"
        ]
        if not texts or texts[-1].strip() != "42":
            raise RuntimeError("Subscription foreground did not remain available during gateway outage")
        status = await host.runtime.invoke(ModelRequest("status", {}))
        if status["pending"] != 0:
            raise RuntimeError("Failed inference leaked a queue slot")
        result = {
            "passed": True,
            "error_type": error,
            "receipt": duplicate,
            "subscription_survived": True,
            "queue_released": True,
        }
        private_write(output / "gateway-outage.json", json.dumps(result, indent=2))
        return result
    finally:
        await docker("up", "-d")
        async with asyncio.timeout(45):
            while True:
                try:
                    if (await host.runtime.invoke(ModelRequest("health", {}), timeout=2)).get("reachable"):
                        break
                except Exception:
                    pass
                await asyncio.sleep(0.5)


async def context(host, output):
    """Bounded long synthetic coding context plus actual native compaction."""
    from scripts.verify_model_workflows import wait_for

    api = await host.tasks.executor.api_jobs()
    client = api.client
    servers = await api.backend.integrations.capture("maintenance", cwd=host.config.workspace)
    reply = await client.request(
        "thread/start",
        {
            "model": "go-anthropic",
            "modelProvider": "assistant_api",
            "allowProviderModelFallback": False,
            "cwd": host.config.workspace,
            "approvalPolicy": host.config.approval_policy,
            "baseInstructions": "Synthetic coding context retention acceptance. Never use tools. Retain the three acceptance constants while compacting; unrelated generated settings are distractors.",
            "config": {"mcp_servers": servers, "web_search": "disabled"},
        },
    )
    thread = reply["thread"]["id"]
    events = []

    async def turn(text):
        offset = len(host.api_events)
        response = await client.request(
            "turn/start", {"threadId": thread, "effort": "none", "input": [{"type": "text", "text": text}]}
        )
        ident = response["turn"]["id"]
        end = await wait_for(
            lambda: next(
                (
                    e
                    for e in host.api_events[offset:]
                    if e.get("method") == "turn/completed"
                    and e.get("params", {}).get("threadId") == thread
                    and e["params"]["turn"]["id"] == ident
                ),
                None,
            ),
            timeout=120,
        )
        batch = [e for e in host.api_events[offset:] if e.get("params", {}).get("threadId") == thread]
        events.extend(batch)
        if end["params"]["turn"]["status"] != "completed":
            raise RuntimeError("Long context turn did not complete")
        if any(
            e.get("method") == "item/completed"
            and e["params"].get("item", {}).get("type") in {"commandExecution", "mcpToolCall", "fileChange"}
            for e in batch
        ):
            raise RuntimeError("Context-only test unexpectedly executed a tool")
        return "\n".join(
            e["params"]["item"].get("text", "")
            for e in batch
            if e.get("method") == "item/completed"
            and e["params"].get("item", {}).get("type") == "agentMessage"
        )

    lines = ['acceptance_start = "START-7331"']
    for i in range(750):
        if i == 375:
            lines.append('acceptance_middle = "MIDDLE-8426"')
        lines.append(f'synthetic_config_{i:04d} = "{hashlib.sha256(str(i).encode()).hexdigest()}"')
    lines.append('acceptance_end = "END-9157"')
    text = (
        "Synthetic generated software configuration. Remember only the three acceptance_* constants for later; reply READY.\n"
        + "\n".join(lines)
    )
    try:
        await turn(text)
        offset = len(host.api_events)
        await client.request("thread/compact/start", {"threadId": thread})
        await wait_for(
            lambda: next(
                (
                    e
                    for e in host.api_events[offset:]
                    if e.get("params", {}).get("threadId") == thread
                    and (
                        e.get("method") == "thread/compacted"
                        or e.get("method") == "item/completed"
                        and e["params"].get("item", {}).get("type") == "contextCompaction"
                    )
                ),
                None,
            ),
            timeout=120,
        )
        events.extend(e for e in host.api_events[offset:] if e.get("params", {}).get("threadId") == thread)
        answer = await turn(
            "Return the three acceptance constant values in start,middle,end order, comma-separated. No tools."
        )
        if not all(value in answer for value in ("START-7331", "MIDDLE-8426", "END-9157")):
            raise RuntimeError("Compacted long context lost an acceptance constant")
        result = {
            "passed": True,
            "input_characters": len(text),
            "answer": answer,
            "thread_id": thread,
            "scope": "750 generated coding settings with three retained markers; not maximum-context capacity",
        }
        private_write(
            output / "context.json", json.dumps({**result, "events": events}, ensure_ascii=False, indent=2)
        )
        return result
    finally:
        summary = await client.request("thread/read", {"threadId": thread, "includeTurns": True})
        for row in summary["thread"].get("turns", []):
            if row.get("status") == "inProgress":
                await client.request("turn/interrupt", {"threadId": thread, "turnId": row["id"]})
        await client.request("thread/archive", {"threadId": thread})


async def performance(host, output):
    from assistant_runtime.kernel.models import ModelRequest

    lag, pending = [], []

    async def heartbeat():
        while True:
            started = time.monotonic()
            await asyncio.sleep(0.02)
            lag.append(max(0, (time.monotonic() - started - 0.02) * 1000))
            pending.append((await host.runtime.invoke(ModelRequest("status", {})))["pending"])

    timer = asyncio.create_task(heartbeat())
    started = time.monotonic()
    try:
        results = await asyncio.gather(
            *[
                host.runtime.invoke(
                    ModelRequest(
                        "infer",
                        {
                            "request_id": str(uuid.uuid4()),
                            "model": model,
                            "text": "Synthetic coding timing check: what is the value of Python 17+25? Reply only 42.",
                        },
                    ),
                    timeout=90,
                )
                for model in ("go-responses", "go-chat", "go-anthropic", "go-gemini")
            ]
        )
        if any(r.get("state") != "completed" or r.get("text", "").strip() != "42" for r in results):
            raise RuntimeError("Concurrent model result mismatch")
        if max(pending, default=0) != 4:
            raise RuntimeError("Four calls were not concurrently admitted")
        evidence = {
            "passed": True,
            "results": results,
            "wall_ms": round((time.monotonic() - started) * 1000),
            "event_loop_max_lag_ms": round(max(lag, default=0), 2),
            "event_loop_p95_lag_ms": round(sorted(lag)[int(len(lag) * 0.95)], 2),
            "max_pending": max(pending),
            "host_peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
            "scope": "Four synthetic concurrent calls; observational sample, not a production latency SLO",
        }
        private_write(output / "performance.json", json.dumps(evidence, ensure_ascii=False, indent=2))
        return evidence
    finally:
        timer.cancel()
        await asyncio.gather(timer, return_exceptions=True)


async def search(host, output):
    from scripts.verify_model_workflows import create, finished, wait_for

    client = LocalRuntimeClient(host.config.socket)
    task = await create(
        client,
        "go-anthropic",
        "Synthetic public documentation search",
        "Coding compatibility test: actually use the available search_service.web_run tool "
        "to search official docs.python.org for asyncio.TaskGroup exception behavior. "
        "Return one sentence explaining what happens to sibling tasks when a task fails, "
        "with the official documentation URL. Do not answer from memory. "
        "Use only that public search tool; no shell, files, personal apps or other MCP.",
    )
    ended = await wait_for(lambda: finished(host, task["id"]))
    trace = await client.invoke(TaskRequest("trace", {"run_id": ended["id"]}))
    calls = [i for i in trace["items"] if i["phase"] == "completed"]
    if (
        ended["state"] != "completed"
        or not calls
        or any(
            i.get("server") != "codex_apps"
            or "search_service" not in i.get("tool", "")
            or i.get("status") != "completed"
            for i in calls
        )
    ):
        raise RuntimeError("Native subscribed search was not successfully used by the API task")
    if "docs.python.org" not in ended["data"].get("result", ""):
        raise RuntimeError("Search result did not retain the official source URL")
    private_write(
        output / "search.json", json.dumps({"run": ended, "trace": trace}, ensure_ascii=False, indent=2)
    )
    return {
        "passed": True,
        "run_id": ended["id"],
        "tool_calls": len(calls),
        "requires_subscription_search": True,
    }


async def memory(host, output):
    from assistant_runtime.bootstrap import memory_complete
    from assistant_runtime.kernel.contracts import MemoryRequest
    from assistant_runtime.kernel.runtime import Runtime
    from assistant_runtime.memory.mem0 import Mem0Memory
    from assistant_runtime.memory.service import MemoryPlugin

    config = replace(host.config, memory_auto_capture=False)
    cache = Path(__file__).resolve().parents[1] / "models/memory-embedding"
    provider = Mem0Memory(
        host.root / "mem0",
        cache,
        lambda messages, schema: memory_complete(host, messages, schema),
        host.config.workspace,
    )
    policy = MemoryPlugin(config, host.store, provider, lambda: False)
    runtime = Runtime(host.store)
    runtime.add(policy)

    async def invoke(action, **arguments):
        return await runtime.invoke(MemoryRequest(action, arguments), timeout=300)

    async def batch(text, role="user"):
        host.store.transcript(role, text)
        host.store.enqueue_memory()
        job = host.store.db.execute(
            "SELECT id,start_id,end_id,epoch FROM memory_jobs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        await policy.process_job(job)
        return job

    def calls():
        return host.store.db.execute("SELECT COUNT(*) FROM model_calls").fetchone()[0]

    evidence = {}
    try:
        await runtime.start()
        first_job = await batch("我的编程代码缩进偏好是两个空格，不用制表符。")
        initial = (await invoke("export"))["records"]
        if not initial or not any("两" in r["text"] or "2" in r["text"] for r in initial):
            raise RuntimeError("API Mem0 did not extract the synthetic coding preference")
        await batch("我写程序时一直偏好两个空格缩进，不使用制表符。")
        repeated = (await invoke("export"))["records"]
        if {r["id"] for r in initial} != {r["id"] for r in repeated}:
            raise RuntimeError("API Mem0 duplicated an unchanged fact")
        await batch("更正我的编程偏好：现在只用四个空格缩进，不再用两个空格，仍然不用制表符。")
        changed = (await invoke("export"))["records"]
        positives = {r["id"]: r["text"] for r in initial if "两" in r["text"] or "2" in r["text"]}
        if not any("四" in r["text"] or "4" in r["text"] for r in changed) or any(
            positives.get(r["id"]) == r["text"] for r in changed
        ):
            raise RuntimeError("API Mem0 did not replace the superseded preference")
        await provider.close()
        recalled = (await invoke("search", query="代码缩进空格偏好", scope="user"))["records"]
        if not any("四" in r["text"] or "4" in r["text"] for r in recalled):
            raise RuntimeError("Memory restart lost the corrected preference")
        before = calls()
        await batch("模型猜测用户编程喜欢 Rust。", role="assistant")
        await batch("已完成：模型猜测用户编程喜欢 Rust。", role="verified")
        if calls() != before:
            raise RuntimeError("Untrusted reports reached memory extraction")
        await batch("假设举例：如果我喜欢 Rust 应该怎样做？我没有说我喜欢 Rust。测试服务器现在临时离线。")
        records = (await invoke("export"))["records"]
        if any("Rust" in r["text"] or "离线" in r["text"] for r in records):
            raise RuntimeError("Hypothetical/temporary content became a persistent fact")
        target = next(r for r in records if "四" in r["text"] or "4" in r["text"])
        corrected = await invoke(
            "remember",
            id=target["id"],
            revision=target["revision"],
            title="代码缩进",
            text="用户编程偏好四个空格缩进。",
            scope="user",
        )
        await invoke("forget", id=target["id"])
        await policy.process_job(first_job)
        await invoke("import", records=[corrected["record"]])
        if (await invoke("read", id=target["id"]))["record"] is not None:
            raise RuntimeError("Deleted memory revived after stale replay/import")
        await batch("关于当前软件项目，我们确定部署服务统一使用 Python 3.12，只维护一个虚拟环境。")
        project = (await invoke("search", query="项目 Python 版本", scope="project"))["records"]
        user = (await invoke("search", query="项目 Python 版本", scope="user"))["records"]
        if not any("3.12" in r["text"] for r in project) or {r["id"] for r in project} & {
            r["id"] for r in user
        }:
            raise RuntimeError("Project facts did not retain scope isolation")
        evidence = {
            "passed": True,
            "initial": initial,
            "corrected": changed,
            "recall": recalled,
            "project": project,
            "calls": calls(),
            "dedup": True,
            "source_validation": True,
            "delete_stale_replay": True,
            "restart": True,
        }
        return {k: v for k, v in evidence.items() if k not in {"initial", "corrected", "recall", "project"}}
    finally:
        private_write(output / "memory.json", json.dumps(evidence, ensure_ascii=False, indent=2))
        await runtime.close()


async def clarification(host, output):
    from scripts.verify_model_workflows import create, finished, wait_for

    client = LocalRuntimeClient(host.config.socket)
    task = await create(
        client,
        "go-anthropic",
        "Synthetic user clarification",
        "Synthetic coding acceptance. The output format has deliberately not been specified. "
        "Use request_user_input to ask one question choosing JSON or CSV; do not assume an answer. "
        "After the user answers, return exactly FORMAT=<chosen format>. No shell, network or other tools.",
    )

    def waiting():
        rows = host.tasks.repo.runs(task["id"], limit=1)
        if not rows:
            return None
        row = rows[0]
        if row["state"] in {"completed", "failed", "unknown"}:
            raise RuntimeError(
                "Task ended without asking: " + json.dumps(row["data"], ensure_ascii=False)[:180]
            )
        return row if row["state"] == "waiting_user" else None

    run = await wait_for(waiting, timeout=90)
    if not run["data"].get("questions"):
        raise RuntimeError("Missing native question payload")
    await asyncio.sleep(1)
    if host.tasks.repo.run(run["id"])["state"] != "waiting_user":
        raise RuntimeError("Clarification did not retain waiting state")
    await client.invoke(TaskRequest("supplement", {"run_id": run["id"], "text": "CSV"}))
    ended = await wait_for(lambda: finished(host, task["id"]))
    if ended["state"] != "completed" or "FORMAT=CSV" not in ended["data"].get("result", ""):
        raise RuntimeError("User answer was not incorporated into completion")
    if ended["data"]["thread_id"] != run["data"]["thread_id"]:
        raise RuntimeError("Answer unexpectedly created another execution context")
    private_write(
        output / "clarification.json",
        json.dumps({"waiting": run, "completed": ended}, ensure_ascii=False, indent=2),
    )
    return {"passed": True, "run_id": run["id"], "same_thread": True}


async def upgrade(host, output):
    from scripts.verify_model_workflows import create, finished, wait_for

    host.tasks.model_policy = ModelPolicy.parse(
        {
            "profiles": {
                "fast": {"model": "go-anthropic", "reasoning_effort": "none"},
                "balanced": {"model": "go-responses", "reasoning_effort": "none"},
            }
        }
    )
    first = host.root / "checkpoint.txt"
    final = host.root / "handoff.txt"
    checkpoint = {
        "completed": [
            "The synthetic checkpoint file "
            + str(first)
            + " already contains VERIFIED-7331. Do not write it again."
        ],
        "next_step": "Read "
        + str(first)
        + "; if its content is VERIFIED-7331, create "
        + str(final)
        + " containing HANDOFF-7331. Then report completion. Use no MCP or network and never request another upgrade.",
        "active_operations": [],
        "uncertain_operations": [],
    }
    args = {
        "request_id": str(uuid.uuid4()),
        "profile": "balanced",
        "cause": "unfamiliar_workflow",
        "reason": "Explicit synthetic acceptance of a cross-provider checkpoint boundary",
        "evidence": "Controlled compatibility fixture requires the next model to independently verify a file checkpoint",
        "checkpoint": checkpoint,
    }
    task = await create(
        LocalRuntimeClient(host.config.socket),
        "go-anthropic",
        "Synthetic cross-provider upgrade",
        "Explicit synthetic handoff test; not a real reasoning limitation. First create "
        + str(first)
        + " containing VERIFIED-7331 exactly and read it back. No background processes. "
        "Then call tasks MCP request_task_model_upgrade with your own run_id (from system task identity) and "
        + json.dumps(args)
        + ". If accepted, end the turn immediately and do not create any further files. "
        "The next model alone must complete the checkpoint next_step. Do not spawn tasks or use other MCP/network.",
    )
    ended = await wait_for(lambda: finished(host, task["id"]), timeout=180)
    history = ended["data"].get("model_upgrades", [])
    if ended["state"] != "completed" or len(history) != 1 or history[0]["phase"] != "applied":
        raise RuntimeError("Cross-provider upgrade was not completed at an applied boundary")
    if first.read_text().strip() != "VERIFIED-7331" or final.read_text().strip() != "HANDOFF-7331":
        raise RuntimeError("Checkpoint evidence did not survive handoff")
    if ended["data"]["spec"]["execution"]["model"] != "go-responses":
        raise RuntimeError("Final model does not match target profile")
    previous = ended["data"].get("previous_thread_id")
    if not previous or previous == ended["data"]["thread_id"]:
        raise RuntimeError("Cross-provider handoff reused private provider context")
    private_write(output / "upgrade.json", json.dumps(ended, ensure_ascii=False, indent=2))
    return {"passed": True, "run_id": ended["id"], "new_thread": True}
