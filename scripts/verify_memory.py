"""Repeatable real Mem0 + ChatGPT-subscribed Codex acceptance, isolated from user facts."""

import argparse
import asyncio
import dataclasses
import json
from pathlib import Path
import tempfile
import time

from assistant_runtime.config import Config
from assistant_runtime.kernel.contracts import MemoryRequest
from assistant_runtime.kernel.runtime import Runtime
from assistant_runtime.memory.mem0 import Mem0Memory
from assistant_runtime.memory.service import MemoryPlugin
from assistant_runtime.plugins.codex.backend import CodexBackend
from assistant_runtime.store import Store


async def verify(config, rounds, folder):
    folder.mkdir(parents=True, exist_ok=True)
    result = {"started_at": time.time(), "engine": "mem0", "rounds": [], "uses_model_http_proxy": False}
    with tempfile.TemporaryDirectory(prefix="mem0-test-", dir=config.state_dir) as temp:
        root = Path(temp)
        workspace = root / "project"
        workspace.mkdir()
        isolated = dataclasses.replace(
            config, root=root, workspace=str(workspace), codex_sandbox="read-only", memory_auto_capture=False
        )
        store = Store(isolated.state_dir / "acceptance.sqlite3")

        async def quiet(*args):
            pass

        backend = CodexBackend(isolated, store, quiet, quiet)
        provider = Mem0Memory(
            root / "mem0", config.models / "memory-embedding", backend.memory_complete, str(workspace)
        )
        policy = MemoryPlugin(isolated, store, provider, lambda: backend.busy)
        runtime = Runtime(store)
        runtime.add(policy)
        server = None
        calls = []
        original_get = backend.client.events.get

        async def trace():
            event = await original_get()
            if event.get("method") in {"turn/started", "turn/completed", "error"}:
                params = event.get("params", {})
                turn = params.get("turn", {})
                print(
                    json.dumps(
                        {
                            "provider_event": event["method"],
                            "status": turn.get("status"),
                            "error": params.get("error"),
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
            item = event.get("params", {}).get("item", {})
            if event.get("method") == "item/completed" and item.get("type") == "mcpToolCall":
                calls.append(
                    {
                        "server": item.get("server"),
                        "tool": item.get("tool"),
                        "status": item.get("status"),
                        "arguments": item.get("arguments"),
                    }
                )
            return event

        backend.client.events.get = trace

        async def invoke(action, **arguments):
            return await runtime.invoke(MemoryRequest(action, arguments), timeout=300)

        async def connection(reader, writer):
            try:
                request = json.loads(await asyncio.wait_for(reader.readline(), 5))
                if request.get("command") != "memory":
                    raise ValueError("This acceptance endpoint exposes memory only")
                answer = await invoke(request["action"], **request.get("arguments", {}))
                reply = {"ok": True, "result": answer}
            except Exception as exc:
                reply = {"ok": False, "error": str(exc)[:300]}
            writer.write((json.dumps(reply, ensure_ascii=False) + "\n").encode())
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        async def batch(text, role="user"):
            store.transcript(role, text)
            store.enqueue_memory()
            job = store.db.execute(
                "SELECT id,start_id,end_id,epoch FROM memory_jobs ORDER BY id DESC LIMIT 1"
            ).fetchone()
            await policy.process_job(job)
            return job

        try:
            await runtime.start()
            isolated.socket.parent.mkdir(parents=True, exist_ok=True)
            server = await asyncio.start_unix_server(connection, path=isolated.socket)
            await backend.start()
            account = await backend.account()
            assert account.get("type") == "chatgpt", "Acceptance must not use an API key account"
            result["account_type"] = account["type"]
            result["foreground_model"] = backend.resolved_model
            result["foreground_reasoning_effort"] = backend.resolved_reasoning_effort
            result["requested_model"] = isolated.memory_model
            result["requested_reasoning_effort"] = isolated.memory_reasoning_effort
            scenarios = [
                (
                    "我平时喜欢喝乌龙茶，不加糖。",
                    "我喝茶通常选乌龙茶，并且不放糖。",
                    "更正一下，我现在只喝绿茶，不再喝乌龙茶，仍然不加糖。",
                    "乌龙茶",
                    "绿茶",
                ),
                (
                    "我最喜欢的水果是苹果。",
                    "我选水果通常选苹果，这是我的个人偏好。",
                    "更正一下，我最喜欢的水果现在是梨，不是苹果。",
                    "苹果",
                    "梨",
                ),
            ]
            for index in range(rounds):
                original, repeated, correction, old_keyword, new_keyword = scenarios[index % len(scenarios)]
                print(f"round {index + 1}: extract", flush=True)
                first_job = await batch(original)
                first = (await invoke("export"))["records"]
                assert any(old_keyword in r["text"] for r in first), first
                first_ids = {r["id"] for r in first}
                print(f"round {index + 1}: repeat and deduplicate", flush=True)
                await batch(repeated)
                repeated_records = (await invoke("export"))["records"]
                assert {r["id"] for r in repeated_records} == first_ids, repeated_records
                print(f"round {index + 1}: correct", flush=True)
                await batch(correction)
                changed = (await invoke("export"))["records"]
                assert any(new_keyword in r["text"] for r in changed), changed
                old_positive = {r["id"]: r["text"] for r in first if old_keyword in r["text"]}
                assert not any(
                    r["id"] in old_positive and r["text"] == old_positive[r["id"]] for r in changed
                ), changed
                print(f"round {index + 1}: restart and recall", flush=True)
                await provider.close()
                recalled = (await invoke("search", query=f"我现在偏好{new_keyword}吗？", scope="user"))[
                    "records"
                ]
                assert any(new_keyword in r["text"] for r in recalled), recalled
                before_calls = backend.memory_model_calls
                await batch("模型猜测用户住在北京。", role="assistant")
                await batch("已完成：模型猜测用户住在北京。", role="verified")
                assert backend.memory_model_calls == before_calls
                await batch("假设举例：如果我住在北京应该怎样做？我没有说我住在北京。现在客厅音箱暂时断线。")
                after_negative = (await invoke("export"))["records"]
                assert not any("北京" in r["text"] or "断线" in r["text"] for r in after_negative), (
                    after_negative
                )
                print(f"round {index + 1}: explicit correction, forget and stale replay", flush=True)
                target = next(r for r in after_negative if new_keyword in r["text"])
                explicit = await invoke(
                    "remember",
                    id=target["id"],
                    revision=target["revision"],
                    title="验收偏好",
                    text=f"用户当前偏好{new_keyword}。",
                    scope="user",
                )
                await invoke("forget", id=target["id"])
                await policy.process_job(first_job)
                await invoke("import", records=[explicit["record"]])
                assert (await invoke("read", id=target["id"]))["record"] is None
                assert target["id"] not in {r["id"] for r in (await invoke("export"))["records"]}
                for record in (await invoke("export"))["records"]:
                    await invoke("forget", id=record["id"])
                assert policy.context_text() == ""
                result["rounds"].append(
                    {
                        "round": index + 1,
                        "initial": first,
                        "corrected": changed,
                        "recall": recalled,
                        "dedup": True,
                        "assistant_report_excluded": True,
                        "negative_input_excluded": True,
                        "forget_and_stale_replay": True,
                    }
                )
            print("project scope and actual Codex MCP search", flush=True)
            await batch("关于当前软件项目，我们确定部署服务统一使用 Python 3.12，只维护一个虚拟环境。")
            project_records = (await invoke("search", query="项目 Python 版本", scope="project"))["records"]
            assert any("3.12" in r["text"] for r in project_records), project_records
            user_records = (await invoke("search", query="项目 Python 版本", scope="user"))["records"]
            assert not any(r["id"] in {p["id"] for p in project_records} for r in user_records)
            await backend.submit(
                '本次只做记忆查询验收。必须实际调用 memory MCP 的 search_memory 工具，query="项目 Python 版本"，scope="project"；根据结果回答项目 Python 版本。禁止 shell、其他工具和任何修改。'
            )
            async with asyncio.timeout(120):
                while backend.busy:
                    await asyncio.sleep(0.1)
            assert any(
                c["server"] == "memory" and c["tool"] == "search_memory" and c["status"] == "completed"
                for c in calls
            ), calls
            task = store.recent(1)[0]
            assert task["status"] == "completed" and "3.12" in task["result"], task
            result.update(
                project_scope_isolated=True,
                codex_mcp_calls=calls,
                codex_result=task["result"],
                model_calls=backend.memory_model_calls,
                model=backend.resolved_memory_model,
                reasoning_effort=backend.resolved_memory_reasoning_effort,
                passed=True,
            )
        except BaseException as exc:
            result.update(passed=False, error=f"{type(exc).__name__}: {exc}")
            raise
        finally:
            if server:
                server.close()
                await server.wait_closed()
            await runtime.close()
            await backend.close()
            store.db.close()
            result["finished_at"] = time.time()
            (folder / "subscription-acceptance.json").write_text(
                json.dumps(result, ensure_ascii=False, indent=2)
            )
            print(
                json.dumps({k: result[k] for k in result if k != "rounds"}, ensure_ascii=False, indent=2),
                flush=True,
            )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.toml")
    parser.add_argument("--rounds", type=int, choices=range(2, 6), default=2)
    args = parser.parse_args()
    config = Config.load(args.config)
    asyncio.run(verify(config, args.rounds, config.state_dir / "acceptance/mem0"))


if __name__ == "__main__":
    main()
