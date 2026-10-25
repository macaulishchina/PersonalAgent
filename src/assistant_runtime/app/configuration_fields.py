"""Compose stored fields from owner data without importing inactive implementations."""

from copy import deepcopy
from dataclasses import field, make_dataclass
from pathlib import Path
import json


def module_fields():
    definitions = []
    kinds = {"string": str, "boolean": bool, "integer": int, "number": float, "object": dict, "array": list}
    root = Path(__file__).resolve().parents[1] / "core"
    for owner in ("speech", "devices", "memory", "workfiles"):
        data = json.loads((root / owner / "configuration.json").read_text(encoding="utf-8"))
        for key, shape in data["schema"]["properties"].items():
            default = shape["default"]
            definitions.append(
                (key, kinds[shape["type"]], field(default_factory=lambda default=default: deepcopy(default)))
            )
    return make_dataclass("ModuleSettings", definitions)


ModuleSettings = module_fields()
