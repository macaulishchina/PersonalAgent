"""Optional business-operation observations; owners retain the authoritative ledger."""

from collections import Counter
import math


STATES = frozenset({"pending", "completed", "failed", "cancelled", "unknown", "waiting_user"})


def summarize(items):
    """Aggregate owner observations, never infer business success from agent prose."""
    counts = Counter(item["state"] for item in items)
    if set(counts) - STATES:
        raise ValueError("Invalid operation outcome")
    state = (
        "waiting_user"
        if counts["waiting_user"]
        else "pending"
        if counts["pending"]
        else "unknown"
        if counts["unknown"]
        else "partial"
        if counts["completed"] and (counts["failed"] or counts["cancelled"])
        else "failed"
        if counts["failed"]
        else "cancelled"
        if counts["cancelled"]
        else "completed"
    )
    return {"state": state, "total": len(items), "counts": dict(counts)}


def validate_observations(value):
    if not isinstance(value, dict) or not isinstance(value.get("operations"), list):
        raise ValueError("Operation provider must return observations")
    items = value["operations"]
    if len(items) > 1000:
        raise ValueError("Operation observation limit exceeded")
    seen = set()
    for item in items:
        if not isinstance(item, dict) or item.get("state") not in STATES:
            raise ValueError("Invalid operation observation")
        ident = item.get("id")
        if not isinstance(ident, str) or not ident or len(ident) > 200 or ident in seen:
            raise ValueError("Operation identity must be bounded and unique")
        seen.add(ident)
        if (
            type(item.get("revision")) not in (int, float)
            or not math.isfinite(item["revision"])
            or item["revision"] < 0
        ):
            raise ValueError("Operation observation requires a revision")
        if item.get("responsibility") not in {"system", "user", "none"}:
            raise ValueError("Operation responsibility is required")
        for field in ("summary", "code", "next_action"):
            if field in item and (not isinstance(item[field], str) or len(item[field]) > 1000):
                raise ValueError("Operation observation text exceeds bounds")
    return summarize(items)
