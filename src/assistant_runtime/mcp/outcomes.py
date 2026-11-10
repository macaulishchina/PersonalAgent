"""Carry owner-confirmed failures through the standard MCP result envelope."""

from fastmcp.server.middleware import Middleware
from fastmcp.tools.base import ToolResult
import logging
from assistant_runtime.api.tool_calls import ToolFailure, ToolOutcome
from assistant_runtime.api.models import ModelCallError
from .contracts import validate

log = logging.getLogger(__name__)


class ToolOutcomes(Middleware):
    def __init__(self, admit=None):
        self.admit = admit

    async def on_call_tool(self, context, call_next):
        dispatched = False
        read_only = False
        try:
            if self.admit:
                self.admit(context.message.name)
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
                hints = tool.annotations
                read_only = bool(hints and hints.read_only_hint is True and hints.destructive_hint is False)
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
            log.warning(
                "Tool handler error tool=%s class=%s read_only=%s",
                context.message.name,
                type(exc).__name__,
                read_only,
            )
            message = (
                "Read did not return a usable result; retry the read once or correct its inputs. No write operation was requested."
                if read_only
                else "Tool result could not be confirmed. Inspect its availability or operation receipt before repeating the call."
            )
            return ToolResult(
                content=message,
                structured_content={
                    "tool_outcome": ToolOutcome(
                        "tool_read_failed"
                        if dispatched and read_only
                        else "tool_result_unconfirmed"
                        if dispatched
                        else "tool_unavailable",
                        "return" if dispatched else "preparation",
                        "dispatched" if dispatched else "not_dispatched",
                        "unknown" if dispatched and not read_only else "failed",
                        message,
                        next_action="retry_read"
                        if read_only
                        else "inspect_operation"
                        if dispatched
                        else "inspect_definition",
                    ).public()
                },
                is_error=True,
            )
