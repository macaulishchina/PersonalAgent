"""Normalize Apple Home measurement values at the adapter boundary."""

from datetime import datetime
import math
import re
import time

from ...kernel.contracts import SensorReading

UNITS = {"temperature": "°C", "humidity": "%"}
VALUE = re.compile(r"^([+-]?\d+(?:[.,]\d+)?)\s*(°?[CF]|℃|℉|%|摄氏度|华氏度)?$", re.I)


def parse_time(value):
    if type(value) in (int, float):
        timestamp = float(value)
    elif isinstance(value, str) and len(value) <= 64:
        d = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if d.tzinfo is None:
            raise ValueError("Sensor timestamp requires an explicit timezone")
        timestamp = d.timestamp()
    else:
        raise ValueError("observed_at requires epoch seconds or ISO 8601 time")
    if not math.isfinite(timestamp) or timestamp < 0 or timestamp > time.time() + 15:
        raise ValueError("Invalid/future sensor timestamp")
    return timestamp


def normalize(metric, raw, observed_at):
    status = "valid"
    unit = None
    if isinstance(raw, dict):
        if set(raw) - {"value", "unit", "status"}:
            raise ValueError("Unknown sensor reading fields")
        status = raw.get("status", "valid")
        unit = raw.get("unit")
        raw = raw.get("value")
    if unit is not None and (not isinstance(unit, str) or not unit):
        raise ValueError("Sensor unit must be a nonempty string")
    if not isinstance(status, str):
        raise ValueError("Sensor status must be a string")
    if raw is None and status == "valid":
        status = "unavailable"
    if status != "valid":
        return SensorReading(metric, raw, UNITS[metric], "Apple Home", observed_at, status)
    if isinstance(raw, str) and len(raw) <= 64:
        match = VALUE.fullmatch(raw.strip())
        if not match:
            raise ValueError("Invalid Apple Home measurement")
        value = float(match[1].replace(",", "."))
        unit = unit or match[2]
    elif type(raw) in (int, float):
        value = float(raw)
    else:
        raise ValueError("Sensor measurement must be a finite number")
    if not math.isfinite(value):
        raise ValueError("Non-finite sensor measurement")
    unit = unit or UNITS[metric]
    if metric == "temperature":
        if unit.upper() in {"F", "°F", "℉", "华氏度"}:
            value = (value - 32) / 1.8
        elif unit.upper() not in {"C", "°C", "℃", "摄氏度"}:
            raise ValueError("Unsupported temperature unit")
        if not -100 <= value <= 100:
            raise ValueError("Temperature is outside supported indoor sensor range")
    elif unit != "%" or not 0 <= value <= 100:
        raise ValueError("Relative humidity must be 0..100 percent")
    return SensorReading(metric, round(value, 4), UNITS[metric], "Apple Home", observed_at)


def parse_payload(data, expected):
    if not isinstance(data, dict) or not isinstance(data.get("device_id"), str):
        raise ValueError("Sensor payload requires device_id")
    ident = data["device_id"]
    if ident not in expected:
        raise ValueError("Unknown sensor endpoint")
    metrics = expected[ident]
    if set(data) != {"device_id", "observed_at", *metrics}:
        raise ValueError("Payload must contain only configured metrics, device_id and observed_at")
    observed = parse_time(data["observed_at"])
    return ident, {metric: normalize(metric, data[metric], observed) for metric in metrics}
