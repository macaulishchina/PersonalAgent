"""Real subscribed Codex curation + local Basic Memory, no microphone or playback."""

import asyncio
import json
from pathlib import Path
import tempfile

from assistant_runtime.config import Config
from assistant_runtime.memory.basic import BasicMemory
from assistant_runtime.memory.service import MemoryPlugin
from assistant_runtime.kernel.contracts import MemoryRequest
from assistant_runtime.plugins.codex.backend import CodexBackend
from assistant_runtime.store import Store

ROOT = Path(__file__).resolve().parents[1]


async def main():
    with tempfile.TemporaryDirectory(prefix="memory-check-", dir=ROOT / "state") as tmp:
        root = Path(tmp)
        c = Config(root=root, workspace=str(root), memory_enabled=False)
        store = Store(c.state_dir / "assistant.sqlite3")

        async def noop(*args):
            return {"ok": True}

        brain = CodexBackend(c, store, noop, noop)
        provider = BasicMemory(root / "memory")
        # Reuse the already downloaded model; each test keeps independent notes/indexes.
        cache = ROOT / "state/acceptance/refactor-memory/model-cache"
        (root / "memory").mkdir()
        if cache.exists():
            (root / "memory/model-cache").symlink_to(cache.resolve(), target_is_directory=True)
        memory = MemoryPlugin(c, store, provider, lambda: False, brain.reflect)
        report = {}
        try:
            await brain.start()
            await provider.start()
            store.transcript("user", "我平时喜欢喝乌龙茶，不加糖。")
            memory.enqueue()
            job = store.db.execute("SELECT id,start_id,end_id,epoch FROM memory_jobs").fetchone()
            await memory.process_job(job)
            records = await provider.export()
            assert records and "乌龙茶" in records[0]["text"]
            report["automatic_curation"] = True
            # Upstream semantic indexing is asynchronous after a durable Markdown write.
            found = []
            for _ in range(15):
                found = await provider.search("喜欢喝什么茶", "user")
                if found:
                    break
                await asyncio.sleep(1)
            assert found, "Chinese semantic recall failed"
            report["chinese_recall"] = True
            await provider.close()
            await provider.start()
            assert await provider.read(records[0]["id"])
            report["restart_recall"] = True
            record = records[0]
            updated = await memory.handle(
                MemoryRequest(
                    "remember",
                    {
                        "id": record["id"],
                        "revision": record["revision"],
                        "title": "饮茶偏好",
                        "text": "现在更喜欢喝绿茶，不加糖。",
                        "scope": "user",
                    },
                )
            )
            assert "绿茶" in updated["record"]["text"]
            report["correction"] = True
            exported = await memory.handle(MemoryRequest("export"))
            await memory.handle(MemoryRequest("forget", {"id": record["id"]}))
            await memory.process_job(job)
            await memory.handle(MemoryRequest("import", exported))
            assert not await provider.export()
            report["forget_no_resurrection"] = True
            # MCP SDK facade works against the same handler, not a second memory implementation.
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client
            import sys

            calls = []

            async def connection(reader, writer):
                data = json.loads(await reader.readline())
                calls.append(data)
                result = await memory.handle(MemoryRequest(data["action"], data["arguments"]))
                writer.write((json.dumps({"ok": True, "result": result}) + "\n").encode())
                await writer.drain()
                writer.close()
                await writer.wait_closed()

            server = await asyncio.start_unix_server(connection, path=c.socket)
            async with server:
                params = StdioServerParameters(
                    command=sys.executable,
                    args=[str(ROOT / "src/assistant_runtime/plugins/codex/tools.py"), str(c.socket)],
                )
                async with stdio_client(params) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        before_rejected = len(calls)
                        for tool, action in [("memory", "import"), ("device", "shutdown")]:
                            rejected = await session.call_tool(tool, {"action": action})
                            assert rejected.is_error, "Out-of-scope action was accepted"
                        assert len(calls) == before_rejected
                        report["mcp_action_scope_enforced"] = True
                        result = await session.call_tool(
                            "memory",
                            {
                                "action": "remember",
                                "arguments": {
                                    "id": "mcp-proof",
                                    "title": "测试偏好",
                                    "text": "仅用于测试的中文记忆",
                                    "scope": "user",
                                },
                            },
                        )
                        assert not result.is_error
                        assert await provider.read("mcp-proof")
                        await session.call_tool(
                            "memory", {"action": "forget", "arguments": {"id": "mcp-proof"}}
                        )
                report["mcp_facade"] = True
                before_calls = len(calls)
                await brain.submit(
                    "请使用 personal_agent 的 memory 工具 search 查询饮茶偏好，scope=user。只报告查询结果，不要调用 shell。"
                )
                for _ in range(120):
                    if not brain.busy:
                        break
                    await asyncio.sleep(0.5)
                assert not brain.busy, "Codex MCP task did not finish"
                assert any(c.get("action") == "search" for c in calls[before_calls:]), (
                    "Codex did not invoke the memory facade"
                )
            report["codex_invokes_memory_mcp"] = True
            report["mcp_facade"] = True
            report["passed"] = True
        except Exception as exc:
            report["passed"] = False
            report["error"] = str(exc)[:300]
            raise
        finally:
            await brain.close()
            await provider.close()
            store.db.close()
            (ROOT / "state/acceptance/refactor-memory.json").write_text(
                json.dumps(report, ensure_ascii=False, indent=2)
            )
            print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
