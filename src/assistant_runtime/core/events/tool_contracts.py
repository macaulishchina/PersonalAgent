"""Event-owned input structures, shared by tools and pre-dispatch validation."""

from assistant_runtime.mcp.contracts import object_schema, nullable

TIME = {"type": "number", "minimum": 0, "exclusiveMaximum": 32503680000}
IDENTIFIER = {"type": "string", "pattern": "^[A-Za-z0-9_.:-]{1,160}$"}
RECORD = object_schema(
    {
        "key": IDENTIFIER,
        "type": IDENTIFIER,
        "subject": IDENTIFIER,
        "version": {"type": "integer", "minimum": 1, "maximum": 1000},
        "evidence": {"enum": ["observation", "baseline", "gap", "user_statement", "assistant_summary"]},
        "data": {
            "type": "object",
            "additionalProperties": True,
            "description": "Event-type-specific data; source rules and total serialized size are validated by the event owner.",
        },
        "occurred_at": nullable(TIME),
        "observed_start": nullable(TIME),
        "observed_end": nullable(TIME),
    },
    ["key", "type", "subject", "version", "evidence", "data"],
    anyOf=[
        {"required": ["occurred_at"], "properties": {"occurred_at": TIME}},
        {
            "required": ["observed_start", "observed_end"],
            "properties": {"observed_start": TIME, "observed_end": TIME},
        },
    ],
)
RECORD["examples"] = [
    {
        "key": "example",
        "type": "user.note",
        "subject": "example",
        "version": 1,
        "evidence": "user_statement",
        "data": {"text": "Synthetic note"},
        "occurred_at": 1700000000,
    }
]

SOURCE = object_schema(
    {
        "adapter": {"type": "string", "minLength": 1},
        "enabled": {"type": "boolean"},
        "config": {
            "type": "object",
            "additionalProperties": True,
            "description": "Owned by the selected trusted adapter. Discover its configuration from event_capabilities before submission.",
        },
    },
    ["adapter", "enabled", "config"],
)
