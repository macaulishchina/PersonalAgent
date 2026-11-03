"""Normalized provider usage, independent of execution transport."""


def telemetry(message):
    usage = message.get("usage")
    if not isinstance(usage, dict) or not any(
        isinstance(usage.get(key), (int, float)) and usage[key] > 0
        for key in ("input", "output", "cacheRead", "cacheWrite")
    ):
        return None
    return {
        "input_tokens": usage.get("input", 0) + usage.get("cacheRead", 0) + usage.get("cacheWrite", 0),
        "output_tokens": usage.get("output", 0),
        # Pi normalizes absent cache counters to zero. A positive count is
        # observed evidence; zero cannot distinguish unreported from a real miss.
        "cached_input_tokens": usage.get("cacheRead") or None,
        "cache_write_input_tokens": usage.get("cacheWrite") or None,
    }
