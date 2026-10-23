"""Explicit live Search MCP acceptance. Public inputs only; query/fetch can incur provider charges."""

import argparse
import asyncio
import json
from pathlib import Path
import sys

from fastmcp import Client

from assistant_runtime.app.config import Config
from assistant_runtime.mcp.catalog import SERVICES


async def verify(args):
    config = Config.load(args.config)
    command = ["-m", "assistant_runtime.mcp", "search", "--socket", str(config.socket)]
    if args.task_run:
        command += ["--task-run", args.task_run]
    async with Client({"mcpServers": {"search": {"command": sys.executable, "args": command}}}) as client:
        tools = {tool.name for tool in await client.list_tools()}
        if tools != set(SERVICES["search"]["tools"]):
            raise RuntimeError("Search tool catalogue mismatch")
        status = (await client.call_tool("search_capabilities")).structured_content
        print(json.dumps({"tools": sorted(tools), "capabilities": status}, ensure_ascii=False), flush=True)
        calls = []
        if args.query:
            calls.append(
                ("search_web", {"query": args.query, "count": 3, "allowed_domains": args.domain or []})
            )
        if args.url:
            calls.append(("fetch_webpage", {"url": args.url, "max_chars": 20000}))
        for tool, arguments in calls:
            if args.provider:
                arguments["provider"] = args.provider
            first = (await client.call_tool(tool, {**arguments, "refresh": True})).structured_content
            if first.get("state") != "completed":
                raise RuntimeError(f"{tool}: {first.get('error', {}).get('code', 'unconfirmed')}")
            cached = (await client.call_tool(tool, arguments)).structured_content
            if status["limits"]["cache_seconds"] and (
                not cached.get("cached") or cached.get("upstream_request_id") != first["upstream_request_id"]
            ):
                raise RuntimeError("Expected same upstream receipt from cache")
            summary = {
                k: first.get(k)
                for k in ("request_id", "provider", "elapsed_ms", "upstream_attempts", "truncated")
            }
            summary.update(
                tool=tool,
                cache_verified=bool(cached.get("cached")),
                results=[{"title": row["title"], "url": row["url"]} for row in first.get("results", [])],
                content_chars=first.get("content_chars"),
            )
            print(json.dumps(summary, ensure_ascii=False), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--query", help="Explicit public test query; incurs provider requests")
    parser.add_argument("--domain", action="append", help="Search domain filter")
    parser.add_argument("--url", help="Explicit public page to fetch; incurs provider requests")
    parser.add_argument("--provider", help="Configured search provider ID")
    parser.add_argument("--task-run", help="Your current active task; never borrow another task's identity")
    asyncio.run(verify(parser.parse_args()))


if __name__ == "__main__":
    main()
