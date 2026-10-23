"""Normalize Apple Home measurement values at the adapter boundary."""

from datetime import datetime
import json
import math
import re
import time

from assistant_runtime.api.contracts import SensorReading

UNITS = {"temperature": "°C", "humidity": "%"}
VALUE = re.compile(r"^([+-]?\d+(?:[.,]\d+)?)\s*(°?[CF]|℃|℉|%|摄氏度|华氏度)?$", re.I)


class PayloadError(ValueError):
    """Fixed validation reason and field, without embedding untrusted input."""

    def __init__(self, message, field=None):
        super().__init__(message)
        self.field = field


def parse_time(value):
    if type(value) in (int, float):
        timestamp = float(value)
    elif isinstance(value, str) and len(value) <= 64:
        try:
            d = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise PayloadError("Sensor timestamp must be ISO 8601 time with timezone") from None
        if d.tzinfo is None:
            raise PayloadError("Sensor timestamp requires an explicit timezone")
        timestamp = d.timestamp()
    else:
        raise PayloadError("observed_at requires epoch seconds or ISO 8601 time")
    if not math.isfinite(timestamp) or timestamp < 0 or timestamp > time.time() + 15:
        raise PayloadError("Invalid/future sensor timestamp")
    return timestamp


def normalize(metric, raw, observed_at):
    status = "valid"
    unit = None
    if isinstance(raw, dict):
        if set(raw) - {"value", "unit", "status"}:
            raise PayloadError("Unknown sensor reading fields")
        status = raw.get("status", "valid")
        unit = raw.get("unit")
        raw = raw.get("value")
    if unit is not None and (not isinstance(unit, str) or not unit):
        raise PayloadError("Sensor unit must be a nonempty string")
    if not isinstance(status, str):
        raise PayloadError("Sensor status must be a string")
    if raw is None and status == "valid":
        status = "unavailable"
    if status != "valid":
        return SensorReading(metric, raw, UNITS[metric], "Apple Home", observed_at, status)
    if isinstance(raw, str) and len(raw) <= 64:
        match = VALUE.fullmatch(raw.strip())
        if not match:
            raise PayloadError("Invalid Apple Home measurement")
        value = float(match[1].replace(",", "."))
        unit = unit or match[2]
    elif type(raw) in (int, float):
        value = float(raw)
    else:
        raise PayloadError("Sensor measurement must be a finite number")
    if not math.isfinite(value):
        raise PayloadError("Non-finite sensor measurement")
    unit = unit or UNITS[metric]
    if metric == "temperature":
        if unit.upper() in {"F", "°F", "℉", "华氏度"}:
            value = (value - 32) / 1.8
        elif unit.upper() not in {"C", "°C", "℃", "摄氏度"}:
            raise PayloadError("Unsupported temperature unit")
        if not -100 <= value <= 100:
            raise PayloadError("Temperature is outside supported indoor sensor range")
    elif unit != "%" or not 0 <= value <= 100:
        raise PayloadError("Relative humidity must be 0..100 percent")
    return SensorReading(metric, round(value, 4), UNITS[metric], "Apple Home", observed_at)


def parse_payload(data, expected):
    if not isinstance(data, dict) or not isinstance(data.get("device_id"), str):
        raise PayloadError("Sensor payload requires device_id", "device_id")
    ident = data["device_id"]
    if ident not in expected:
        raise PayloadError("Unknown sensor endpoint", "device_id")
    metrics = expected[ident]
    if set(data) != {"device_id", "observed_at", *metrics}:
        raise PayloadError(
            "Payload must contain only configured metrics, device_id and observed_at", "fields"
        )
    try:
        observed = parse_time(data["observed_at"])
    except (ValueError, TypeError, OverflowError) as exc:
        reason = str(exc) if isinstance(exc, PayloadError) else "Invalid sensor timestamp"
        raise PayloadError(reason, "observed_at") from None
    readings = {}
    for metric in metrics:
        try:
            readings[metric] = normalize(metric, data[metric], observed)
        except (ValueError, TypeError, OverflowError) as exc:
            reason = str(exc) if isinstance(exc, PayloadError) else "Invalid Apple Home measurement"
            raise PayloadError(reason, metric) from None
    return ident, readings


def diagnostic_fields(data, expected, token):
    """Bounded previews of known sensor fields, never the complete request body."""
    if not isinstance(data, dict) or not isinstance(data.get("device_id"), str):
        return {}
    ident = data["device_id"]
    if ident not in expected:
        return {}
    fields = {}
    for key in ("observed_at", *expected[ident]):
        raw = data.get(key)
        try:
            preview = json.dumps(raw, ensure_ascii=True)
        except (ValueError, TypeError, RecursionError):
            preview = "<unrepresentable>"
        if token:
            preview = preview.replace(token, "[redacted]")
        fields[key] = {
            "type": type(raw).__name__,
            "present": key in data,
            "length": len(raw) if isinstance(raw, (str, list, dict)) else None,
            "preview": preview[:256],
            "truncated": len(preview) > 256,
        }
    return {"device_id": ident, "fields": fields}
