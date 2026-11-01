"""Explicit HID identities and exact report mappings; no inferred device bindings."""

from dataclasses import dataclass
import re
from assistant_runtime.api.configuration import configuration_spec

DEFAULT_DEBOUNCE = configuration_spec(__file__).schema["additionalProperties"]["properties"]["debounce_ms"][
    "default"
]


@dataclass(frozen=True)
class Button:
    name: str
    vendor_id: int
    product_id: int
    serial: str
    report: bytes
    debounce_ms: int


def parse(settings):
    if not isinstance(settings, dict) or len(settings) > 8:
        raise ValueError("hid_buttons must contain at most 8 button definitions")
    result = []
    identities = set()
    for name, data in settings.items():
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,47}", name):
            raise ValueError("Invalid HID button name")
        allowed = {"vendor_id", "product_id", "serial", "report", "action", "debounce_ms"}
        if not isinstance(data, dict) or set(data) - allowed:
            raise ValueError("Unknown HID button configuration fields")
        ids = []
        for key in ("vendor_id", "product_id"):
            value = data.get(key)
            if not isinstance(value, str) or not re.fullmatch(r"[0-9a-fA-F]{4}", value):
                raise ValueError(f"{key} must be a four-digit hexadecimal string")
            ids.append(int(value, 16))
        serial = data.get("serial")
        if not isinstance(serial, str) or not serial.strip() or len(serial) > 128:
            raise ValueError("HID serial is required, at most 128 characters")
        report = data.get("report")
        if not isinstance(report, str) or not re.fullmatch(r"(?:[0-9a-fA-F]{2}){1,64}", report):
            raise ValueError("HID report must be 1..64 bytes encoded as hex, including report ID")
        debounce = data.get("debounce_ms", DEFAULT_DEBOUNCE)
        if type(debounce) is not int or not 100 <= debounce <= 5000:
            raise ValueError("HID debounce_ms must be 100..5000")
        if data.get("action") != "wake":
            raise ValueError("HID button action must be wake")
        identity = (*ids, serial)
        if identity in identities:
            raise ValueError("Only one wake button mapping per HID identity is supported")
        identities.add(identity)
        result.append(Button(name, *ids, serial, bytes.fromhex(report), debounce))
    return result
