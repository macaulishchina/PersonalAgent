"""Read-only usage tools. Allowance changes are never exposed to models."""

from fastmcp import FastMCP
from assistant_runtime.api.ai_usage import UsageRequest
from assistant_runtime.api.annotations import READ


def create_server(client, caller_run=None):
    server = FastMCP(
        "ai_usage",
        instructions="Read AI service consumption and budget status. These tools cannot change limits, grant permissions, resume plans or replay work.",
    )

    @server.tool(annotations=READ)
    async def ai_usage_status() -> dict:
        """Metering health; this does not establish provider availability."""
        return await client.invoke(UsageRequest("status", caller_run=caller_run))

    @server.tool(annotations=READ)
    async def ai_usage_calls(limit: int = 20) -> dict:
        """Recent AI calls; background scope is fixed to this task."""
        return await client.invoke(UsageRequest("calls", {"limit": limit}, caller_run))

    @server.tool(annotations=READ)
    async def ai_usage_summary(work_id: str = "") -> dict:
        """Budget and known/unknown use. A background caller cannot choose another work."""
        return await client.invoke(UsageRequest("summary", {"work_id": work_id}, caller_run))

    return server
