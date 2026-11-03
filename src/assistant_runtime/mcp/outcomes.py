"""Carry owner-confirmed failures through the standard MCP result envelope."""

from fastmcp.server.middleware import Middleware
from fastmcp.tools.base import ToolResult
from assistant_runtime.api.tool_calls import ToolFailure, ToolOutcome
from assistant_runtime.api.models import ModelCallError
from .contracts import validate


class ToolOutcomes(Middleware):
    async def on_call_tool(self, context, call_next):
        dispatched = False
        try:
            # Validate the published owner schema before invoking any handler. Do not
            # classify an arbitrary exception *inside* a handler as non-dispatch.
            if context.fastmcp_context is not None:
                tool = await context.fastmcp_context.fastmcp.get_tool(context.message.name)
                if tool is None:
                    raise ToolFailure(
                        ToolOutcome(
                            "tool_unavailable",
                            "preparation",
                            "not_dispatched",
                            "failed",
                            "Tool is not registered or available",
                            next_action="inspect_definition",
                        )
                    )
                validate(context.message.arguments or {}, tool.parameters, "")
            dispatched = True
            return await call_next(context)
        except Exception as exc:
            # FastMCP may wrap a handler exception; follow causes, never parse text.
            seen = set()
            current = exc
            while current is not None and id(current) not in seen:
                seen.add(id(current))
                if isinstance(current, ModelCallError):
                    problem = current.failure
                    not_sent = problem.get("submission") == "not_submitted"
                    current = ToolFailure(
                        ToolOutcome(
                            current.code,
                            "return",
                            "not_dispatched" if not_sent else "dispatched",
                            "failed" if not_sent or problem.get("generation_settled") else "unknown",
                            problem.get("message") or current.code,
                            next_action="inspect_model_failure",
                        ),
                        result={"failure": problem},
                    )
                if isinstance(current, ToolFailure):
                    value = current.outcome.public()
                    return ToolResult(
                        content=value["message"],
                        structured_content={
                            "tool_outcome": value,
                            **({"received_result": current.result} if current.result is not None else {}),
                        },
                        meta={"personal_agent.tool_outcome": value},
                        is_error=True,
                    )
                current = current.__cause__ or current.__context__
            return ToolResult(
                content="Tool result could not be confirmed. Inspect its availability or operation receipt before repeating the call.",
                structured_content={
                    "tool_outcome": ToolOutcome(
                        "tool_result_unconfirmed" if dispatched else "tool_unavailable",
                        "return" if dispatched else "preparation",
                        "dispatched" if dispatched else "not_dispatched",
                        "unknown" if dispatched else "failed",
                        "Tool result could not be confirmed",
                        next_action="inspect_operation" if dispatched else "inspect_definition",
                    ).public()
                },
                is_error=True,
            )
