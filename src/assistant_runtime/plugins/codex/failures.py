"""Normalize structured native model errors; never parse prose or infer tool safety."""


def model_failure(error):
    info = error.get("codexErrorInfo") if isinstance(error, dict) else None
    allowed = {
        "usageLimitExceeded",
        "rateLimitExceeded",
        "flexUnavailable",
        "serverOverloaded",
        "internalServerError",
        "unauthorized",
    }
    if isinstance(info, str) and info in allowed:
        return {"code": info, "fallback_allowed": True}
    if isinstance(info, dict):
        for variant in (
            "httpConnectionFailed",
            "responseStreamConnectionFailed",
            "responseTooManyFailedAttempts",
        ):
            details = info.get(variant)
            status = details.get("httpStatusCode") if isinstance(details, dict) else None
            if type(status) is int and status in {401, 402, 403, 404, 408, 429, 500, 502, 503, 504}:
                return {"code": f"model_http_{status}", "fallback_allowed": True}
    return {"code": "model_failure_unclassified", "fallback_allowed": False}
