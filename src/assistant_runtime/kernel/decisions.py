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
