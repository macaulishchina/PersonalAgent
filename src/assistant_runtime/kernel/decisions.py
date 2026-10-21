"""Optional decision support; every failed suggestion returns the original route."""


def fallback(reason, receipt_id=None):
    return {
        "status": "fallback",
        "choice": None,
        "reason": reason,
        "fallback": "use_original_logic",
        "receipt_id": receipt_id,
    }


async def decide(runtime, request):
    try:
        result = await runtime.invoke(request)
        if (
            not isinstance(result, dict)
            or result.get("status") not in {"ok", "fallback"}
            or result["status"] == "ok"
            and (not isinstance(result.get("choice"), str) or not result["choice"])
        ):
            return fallback("invalid_decision_result")
        return result
    except Exception:
        # Cancellation is deliberately not caught: shutdown/user cancellation
        # must not launch a second workflow through a fallback.
        return fallback("decision_service_unavailable")


async def report_decision(runtime, result, status, reason, operation_id=None):
    """Best-effort owner receipt; never let optional observability block work."""
    import asyncio
    from .contracts import DecisionOutcome

    ident = result.get("decision_id")
    if not ident:
        return
    try:
        async with asyncio.timeout(0.1):
            await runtime.invoke(DecisionOutcome(ident, status, reason, operation_id))
    except Exception:
        pass


def submit_decision(context, request, consume, *, current):
    """Owned parallel suggestion. A late result never reaches the consumer."""

    async def work():
        result = await decide(context.runtime, request)
        if not current():
            await report_decision(context.runtime, result, "rejected", "stale_input")
            return
        await consume(result)

    return context.spawn(work(), "decision:" + request.kind)
