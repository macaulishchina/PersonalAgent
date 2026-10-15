"""Isolated real Codex MCP transport/scope/refresh acceptance; no model turns or physical actions."""

import argparse
import asyncio
from pathlib import Path
import socket
import sys
import tempfile
from types import SimpleNamespace
import json

import aiohttp

from assistant_runtime.config import Config
from assistant_runtime.plugins.codex.integrations import CodexIntegrations
from assistant_runtime.plugins.codex.rpc import CodexClient
from assistant_runtime.plugins.integrations.registry import McpRegistry

FIXTURE = """from fastmcp import FastMCP
import sys
m = FastMCP("personal-agent-acceptance")
@m.tool()
def echo(value: str) -> str:
    return value
if len(sys.argv) > 1:
    m.run(transport="http", host="127.0.0.1", port=int(sys.argv[1]), show_banner=False)
else:
    m.run(transport="stdio", show_banner=False)
"""


async def verify(binary):
    with tempfile.TemporaryDirectory(prefix="personal-agent-mcp-") as area:
        root = Path(area)
        fixture = root / "fixture.py"
        fixture.write_text("#!" + sys.executable + "\n" + FIXTURE)
        fixture.chmod(0o700)
        with socket.socket() as port_owner:
            port_owner.bind(("127.0.0.1", 0))
            port = port_owner.getsockname()[1]
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            str(fixture),
            str(port),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        client = CodexClient(binary, str(root))
        registry = McpRegistry(root, {})
        cfg = Config(root=root, workspace=str(root), plugins=["codex", "integrations"])
        backend = SimpleNamespace(
            config=cfg,
            client=client,
            submit_lock=asyncio.Lock(),
            busy=False,
            active_turn=None,
            voice_active=lambda: False,
            thread_id=None,
        )
        adapter = CodexIntegrations(backend, registry)
        foreground = None
        try:
            async with aiohttp.ClientSession() as session:
                for _ in range(80):
                    if process.returncode is not None:
                        raise RuntimeError("Synthetic HTTP MCP exited before startup")
                    try:
                        async with session.get(f"http://127.0.0.1:{port}/mcp") as response:
                            if response.status < 500:
                                break
                    except aiohttp.ClientError:
                        pass
                    await asyncio.sleep(0.1)
                else:
                    raise TimeoutError("Synthetic HTTP MCP did not start")
            await client.start()
            await adapter.start()
            registry.configure(
                "stdio",
                {
                    "transport": {
                        "type": "stdio",
                        "command": "./fixture.py",
                        "args": [],
                        "cwd": str(root),
                    },
                    "approval": "approve",
                    "allow_tools": ["echo"],
                },
                0,
            )
            registry.configure(
                "http",
                {
                    "transport": {"type": "http", "url": f"http://127.0.0.1:{port}/mcp"},
                    "contexts": ["background"],
                    "approval": "approve",
                },
                1,
            )
            for name, context in (("stdio", "foreground"), ("stdio", "background"), ("http", "background")):
                result = await adapter.verify(name, context, True)
                assert result["state"] == "verified", result
                assert result["tools_count"] == 1
            try:
                await adapter.verify("http", "foreground")
            except ValueError:
                pass
            else:
                raise AssertionError("Excluded foreground scope was verified")
            # The provider itself dispatches the real synthetic tools, without inference.
            for name, context in (("stdio", "foreground"), ("http", "background")):
                thread = await adapter.open_probe(name, context)
                try:
                    await client.request(
                        "mcpServerStatus/list",
                        {"threadId": thread, "serverName": "external_" + name, "detail": "toolsAndAuthOnly"},
                    )
                    result = await client.request(
                        "mcpServer/tool/call",
                        {
                            "threadId": thread,
                            "server": "external_" + name,
                            "tool": "echo",
                            "arguments": {"value": "acceptance"},
                        },
                    )
                    assert not result.get("isError") and "acceptance" in json.dumps(result), result
                finally:
                    await adapter.close_probe(thread)
            start = await client.request(
                "thread/start",
                {"cwd": str(root), "config": {"mcp_servers": adapter.configuration("foreground")}},
            )
            foreground = backend.thread_id = start["thread"]["id"]
            await client.request(
                "thread/inject_items",
                {
                    "threadId": foreground,
                    "items": [
                        {
                            "type": "message",
                            "role": "user",
                            "content": [
                                {
                                    "type": "input_text",
                                    "text": "Synthetic MCP configuration acceptance; no task to execute.",
                                }
                            ],
                        }
                    ],
                },
            )
            adapter.applied_revision = registry.revision
            registry.configure("stdio", {"enabled": False}, registry.revision)
            assert (await adapter.apply())["state"] == "applied"
            disabled = await client.request(
                "mcpServerStatus/list",
                {"threadId": foreground, "serverName": "external_stdio", "detail": "toolsAndAuthOnly"},
            )
            assert disabled["data"][0]["runtimeStatus"] == "disabled", disabled
            registry.configure("stdio", {"enabled": True}, registry.revision)
            assert (await adapter.apply())["state"] == "applied"
            enabled = await client.request(
                "mcpServerStatus/list",
                {"threadId": foreground, "serverName": "external_stdio", "detail": "toolsAndAuthOnly"},
            )
            assert enabled["data"][0]["runtimeStatus"] == "connected", enabled
            assert backend.thread_id == foreground
            registry.configure(
                "missing",
                {
                    "transport": {"type": "stdio", "command": str(root / "missing-program")},
                    "startup_timeout_seconds": 2,
                },
                registry.revision,
            )
            assert (await adapter.verify("missing", "foreground"))["state"] == "unavailable"
            assert all(not s["enabled"] for s in adapter.configuration("maintenance").values())
            assert not adapter.probes
            return {
                "passed": True,
                "stdio_and_streamable_http": True,
                "foreground_and_background_calls": True,
                "scope_exclusion": True,
                "same_foreground_thread_refreshed": True,
                "missing_command_reported": True,
                "maintenance_tools_disabled": True,
                "model_turns_started": 0,
                "physical_actions": 0,
            }
        finally:
            if foreground:
                try:
                    await client.request("thread/archive", {"threadId": foreground})
                    await client.request("thread/unsubscribe", {"threadId": foreground})
                except Exception:
                    pass
            await client.close()
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), 5)
                except TimeoutError:
                    process.kill()
                    await process.wait()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex", default="codex")
    args = parser.parse_args()
    print(json.dumps(asyncio.run(verify(args.codex)), ensure_ascii=False))


if __name__ == "__main__":
    main()
