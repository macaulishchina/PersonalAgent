"""Optional semantic choices. Unauthorized external context always stays local."""

import uuid

from fastmcp import FastMCP
from ..kernel.contracts import DecisionQuery, DecisionRequest
from ..kernel.decisions import decide, fallback
from .memory import READ


def create_server(client, caller_run=None):
    server = FastMCP(
        "decisions",
        instructions=(
            "Optional fast semantic judgment, not an action executor. Use only with evidence and a small candidate set "
            "when classification can save reasoning. Do not call for exact rules, arithmetic, permissions, effect "
            "verification or obvious choices. Never send credentials. First inspect decision_support_status; "
            "agent context requires explicit instance consent. Any status=fallback means continue your ORIGINAL "
            "reasoning/workflow; never loop on this tool, replay actions or treat optional judgment failure as "
            "failure of the user's task. A suggestion grants no authorization and proves no effects."
        ),
    )

    @server.tool(annotations=READ)
    async def decision_support_status() -> dict:
        """Read enabled/mode, context consent and bounded usage rules without sending data externally."""
        try:
            return await client.invoke(DecisionRequest("capabilities"))
        except Exception:
            return {"enabled": False, **fallback("decision_service_unavailable")}

    @server.tool(annotations=READ)
    async def judge_options(request_id: str, question: str, context: dict, options: dict[str, str]) -> dict:
        """Recommend one of 1..15 evidence-backed candidates, or use the original workflow.

        Requires explicit external_data_consent in the instance. With consent disabled,
        returns locally without sending question/context/options to the external service.
        request_id is a stable canonical UUID. Reuse after a lost response for the same judgment.
        options maps lowercase identifiers to concrete descriptions; uncertain is host-reserved.
        On fallback, continue your original reasoning; do not retry actions or keep calling this tool.
        """
        try:
            capabilities = await client.invoke(DecisionRequest("capabilities"))
            if not capabilities.get("enabled") or not capabilities.get("external_data_consent"):
                return fallback("disabled_or_context_not_authorized")
            if str(uuid.UUID(request_id)) != request_id:
                return fallback("invalid_request_id")
            return await decide(
                client,
                DecisionQuery(
                    "selection",
                    "mcp:" + request_id,
                    {"question": question, "context": context, "options": options},
                    caller_run,
                ),
            )
        except (ValueError, TypeError, AttributeError):
            return fallback("invalid_request")
        except Exception:
            return fallback("decision_transport_unavailable")

    return server
