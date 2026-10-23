"""Public event validation. Time is Unix UTC; intervals express uncertainty."""

import hashlib
import json
import math
from assistant_runtime.kernel.event_reporting import identifier


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def identity(source, key):
    return hashlib.sha256(canonical([source, key]).encode()).hexdigest()


def timestamp(value):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value < 32503680000:
        raise ValueError("Invalid UTC timestamp")
    return value


def validate(record):
    fields = {
        "key",
        "type",
        "version",
        "subject",
        "occurred_at",
        "observed_start",
        "observed_end",
        "evidence",
        "data",
    }
    if not isinstance(record, dict) or set(record) - fields:
        raise ValueError("Unknown event fields")
    for key in ("key", "type", "subject"):
        identifier(record.get(key))
    if type(record.get("version")) is not int or not 1 <= record["version"] <= 1000:
        raise ValueError("Event version must be 1..1000")
    if record.get("evidence") not in {
        "observation",
        "baseline",
        "gap",
        "user_statement",
        "assistant_summary",
    }:
        raise ValueError("Invalid event evidence")
    if not isinstance(record.get("data"), dict):
        raise ValueError("Event data must be an object")
    if record.get("occurred_at") is not None:
        timestamp(record["occurred_at"])
        if record.get("observed_start") is not None or record.get("observed_end") is not None:
            raise ValueError("Choose occurrence time or observation interval")
    else:
        start, end = timestamp(record.get("observed_start")), timestamp(record.get("observed_end"))
        if start > end:
            raise ValueError("Reversed observation interval")
    wire = canonical(record)
    if len(wire.encode()) > 16 * 1024:
        raise ValueError("Event exceeds 16 KiB")
    return json.loads(wire)
