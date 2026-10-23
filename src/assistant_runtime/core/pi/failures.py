"""Only structured executor errors can authorize model fallback."""


def model_failure(error):
    if isinstance(error, dict) and error.get("code") == "ai_usage_blocked":
        return {"code": "ai_usage_blocked", "fallback_allowed": False}
    if isinstance(error, dict) and error.get("code") == "context_compaction_failed":
        return {"code": "context_compaction_failed", "fallback_allowed": False}
    status = error.get("status") if isinstance(error, dict) else None
    if type(status) is int and status in {401, 402, 403, 404, 408, 429, 500, 502, 503, 504}:
        return {"code": f"model_http_{status}", "fallback_allowed": True}
    return {"code": "model_failure_unclassified", "fallback_allowed": False}
