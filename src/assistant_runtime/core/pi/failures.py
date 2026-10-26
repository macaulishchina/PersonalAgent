"""Classify structured executor failures without authorizing another dispatch."""


def model_failure(error):
    if isinstance(error, dict) and error.get("code") == "ai_usage_blocked":
        return {"code": "ai_usage_blocked"}
    if isinstance(error, dict) and error.get("code") == "context_compaction_failed":
        return {"code": "context_compaction_failed"}
    status = error.get("status") if isinstance(error, dict) else None
    if type(status) is int and status in {401, 402, 403, 404, 408, 429, 500, 502, 503, 504}:
        return {"code": f"model_http_{status}"}
    return {"code": "model_failure_unclassified"}
