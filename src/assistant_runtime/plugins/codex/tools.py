"""Codex MCP facade delegates to exactly the same local capability handlers as CLI."""

import asyncio
import json
import sys
from typing import Literal
from fastmcp import FastMCP

mcp = FastMCP("Personal Agent local capabilities")


async def invoke(command, **payload):
    reader, writer = await asyncio.open_unix_connection(sys.argv[1], limit=4 * 1024 * 1024)
    try:
        writer.write((json.dumps({"command": command, **payload}, ensure_ascii=False) + "\n").encode())
        await writer.drain()
        reply = json.loads(await asyncio.wait_for(reader.readline(), 90))
        if not reply["ok"]:
            raise RuntimeError(reply["error"])
        return reply["result"]
    finally:
        writer.close()
        await writer.wait_closed()


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True, "openWorldHint": False})
async def memory(
    action: Literal["search", "read", "remember", "forget"], arguments: dict | None = None
) -> dict:
    """Local long-term memory: search(query,scope), read(id), remember(title,text,scope,id?,revision?), forget(id).
    Store only explicit stable user facts/preferences or verified work results; cite actual evidence.
    Correct using existing id and current revision. Never claim success when this tool fails.
    Scope is user or project. Forget removes long-term memory, not conversation history.
    """
    return await invoke("memory", action=action, arguments=arguments or {})


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True, "openWorldHint": False})
async def device(
    action: Literal["room", "volume", "stop", "sleep", "progress", "cancel", "list", "inspect"],
    value: str | int | None = None,
) -> dict:
    """Device inventory: list (no value), inspect (value=device id); read-only, timestamped availability.
    Audio controls: room(endpoint id), volume(0..100), stop, sleep, progress, cancel.
    stop silences speech but keeps work; sleep ends conversation but keeps work; cancel stops work.
    Only act on user requests. Never use shell to control audio or terminate the assistant.
    """
    if action in {"list", "inspect"}:
        return await invoke("devices", action=action, device_id=value)
    return await invoke(action, value=value)


if __name__ == "__main__":
    mcp.run(transport="stdio")
