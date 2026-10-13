"""Codex MCP facade delegates to exactly the same local capability handlers as CLI."""

import asyncio
import json
import sys
from typing import Literal
from fastmcp import FastMCP
from fastmcp.tools.base import ToolResult
from mcp.types import ImageContent, TextContent

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
    action: Literal["room", "volume", "stop", "sleep", "progress", "cancel", "list", "inspect", "snapshot"],
    value: str | int | None = None,
) -> ToolResult:
    """Device inventory: list (no value), inspect (value=device id); read-only, timestamped availability.
    Audio controls: room(endpoint id), volume(0..100), stop, sleep, progress, cancel.
    snapshot(value=camera id): acquire one fresh JPEG and return its actual image content.
    Only capture on an explicit user request to see the current scene. No continuous video or local recording.
    stop silences speech but keeps work; sleep ends conversation but keeps work; cancel stops work.
    Only act on user requests. Never use shell to control audio or terminate the assistant.
    """
    if action in {"list", "inspect"}:
        return ToolResult(structured_content=await invoke("devices", action=action, device_id=value))
    if action == "snapshot":
        result = await invoke("devices", action=action, device_id=value)
        frame = result["frame"]
        image = frame.pop("data_base64")
        return ToolResult(
            content=[
                TextContent(type="text", text=json.dumps(result, ensure_ascii=False)),
                ImageContent(type="image", data=image, mime_type=frame["mime_type"]),
            ],
            structured_content=result,
        )
    return ToolResult(structured_content=await invoke(action, value=value))


if __name__ == "__main__":
    mcp.run(transport="stdio")
