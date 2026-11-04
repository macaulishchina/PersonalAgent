"""Task-owned schemas shared by MCP discovery and runtime validation."""

from copy import deepcopy
from assistant_runtime.mcp.contracts import nullable


def text(maximum=500):
    return {"type": "string", "minLength": 1, "maxLength": maximum}


def obj(properties, required=()):
    return {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
        **({"required": list(required)} if required else {}),
    }


ASSESSMENT = obj(
    {
        "acceptance": text(),
        "procedure": text(),
        "user_preference": text(),
        "prior_run_id": text(64),
        "uncertainties": {"type": "array", "items": text(300), "maxItems": 4},
    }
)
EXECUTION = obj(
    {
        "kind": {"type": "string", "enum": ["agent", "reminder", "device_query"]},
        "model": nullable(text(160)),
        "reasoning_effort": nullable(text(160)),
        "effort_mode": {"enum": ["model_default"]},
        "expected_model": obj(
            {"model": text(64), "revision": {"type": "string", "pattern": "^[a-f0-9]{64}$"}},
            ["model", "revision"],
        ),
        "recommendation": nullable(text(64)),
        "reason": {
            **nullable(text(400)),
            "description": "Why this model/effort fits the task; required for a concrete choice unless fixed by instance policy.",
        },
        "model_source": {"enum": ["assistant", "user", None]},
        "effort_source": {"enum": ["assistant", "user", None]},
        "device_id": text(200),
        "assessment": ASSESSMENT,
    }
)
EVENT = obj(
    {
        "source": {"enum": ["device", "task", "records"]},
        "id": text(200),
        "metric": text(200),
        "operator": {"enum": ["eq", "gt", "lt"]},
        "value": {},
        "duration_seconds": {"type": "number", "minimum": 0},
        "cooldown_seconds": {"type": "number", "minimum": 0},
        "hysteresis": {"type": "number", "minimum": 0},
        "types": {"type": "array", "items": text(200), "minItems": 1, "maxItems": 16},
        "source_id": text(200),
        "subject": text(200),
        "merge_seconds": {"type": "number", "minimum": 0},
        "max_age_seconds": {"type": "number", "minimum": 0},
        "capacity": {"type": "integer", "minimum": 1},
    },
    ["source"],
)
TRIGGER = obj(
    {
        "kind": {"enum": ["immediate", "date", "interval", "cron", "event"]},
        "at": text(100),
        "seconds": {"type": "number", "minimum": 1, "maximum": 366 * 86400},
        "expression": text(100),
        "timezone": text(100),
        "event": EVENT,
    }
)
TRIGGER["allOf"] = [
    {"if": {"properties": {"kind": {"const": kind}}, "required": ["kind"]}, "then": {"required": [field]}}
    for kind, field in (("date", "at"), ("interval", "seconds"), ("cron", "expression"), ("event", "event"))
]
SELF_COMPLETION = obj({"condition": text(1000)}, ["condition"])
SELF_SCHEDULE = obj(
    {
        "condition": text(1000),
        "allowed_triggers": {"type": "array", "items": TRIGGER, "minItems": 1, "maxItems": 16},
    },
    ["condition", "allowed_triggers"],
)

CHECKPOINT = obj(
    {
        "completed": {"type": "array", "items": text(1000), "maxItems": 16},
        "next_step": text(2000),
        "uncertain_operations": {
            "type": "array",
            "maxItems": 0,
            "description": "Reconcile uncertain operations and wait for active operations before upgrading",
        },
        "active_operations": {
            "type": "array",
            "maxItems": 0,
            "description": "Reconcile uncertain operations and wait for active operations before upgrading",
        },
    },
    ["completed", "next_step", "uncertain_operations", "active_operations"],
)


def patch_schema(schema):
    result = deepcopy(schema)
    result.pop("required", None)
    result.pop("allOf", None)
    if result.get("type") == "object":
        result["properties"] = {
            key: {"anyOf": [patch_schema(value), {"type": "null"}]}
            for key, value in result["properties"].items()
        }
    return result


CHANGES = obj(
    {
        "title": text(200),
        "prompt": text(4000),
        "trigger": patch_schema(TRIGGER),
        "execution": patch_schema(EXECUTION),
        "long_term": {"type": "boolean"},
        "workspace": text(4000),
        "resources": {"type": "array", "items": text(200), "maxItems": 16},
        "resource_locks": {
            "type": "array",
            "items": text(200),
            "maxItems": 16,
            "description": "Explicit scheduling mutex objects. Separate from authorization resources; omit for plugin-owned transaction locking.",
        },
        "notify_voice": {"type": "boolean"},
        "notify_completion": {"type": "boolean"},
        "auto_archive": {"type": "boolean"},
        "misfire_seconds": {"type": "integer", "minimum": 0, "maximum": 86400},
        "self_schedule": {"anyOf": [patch_schema(SELF_SCHEDULE), {"type": "null"}]},
        "self_completion": {"anyOf": [patch_schema(SELF_COMPLETION), {"type": "null"}]},
    }
)
CHANGES["description"] = (
    "Recursive field patch. Omitted fields stay unchanged; null clears optional fields; lists replace. Changing trigger/execution kind starts that variant afresh. Read revision first."
)


def merge_patch(current, patch):
    result = deepcopy(current)
    if "kind" in patch and patch["kind"] != current.get("kind"):
        result = {}
    for key, value in patch.items():
        if value is None:
            result.pop(key, None)
        elif isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge_patch(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result
