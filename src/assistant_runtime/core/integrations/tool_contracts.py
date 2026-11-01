"""MCP registry edit contract; settings owns transport semantics and credentials."""

from assistant_runtime.mcp.contracts import object_schema, nullable

TEXT = {"type": "string"}
STRINGS = {"type": "array", "items": TEXT, "maxItems": 100}
MAPPING = {"type": "object", "additionalProperties": TEXT, "maxProperties": 100}
TRANSPORT = {
    "oneOf": [
        object_schema(
            {
                "type": {"const": "stdio"},
                "command": {"type": "string", "minLength": 1},
                "args": STRINGS,
                "cwd": nullable(TEXT),
                "env": MAPPING,
                "env_vars": STRINGS,
            },
            ["type", "command"],
        ),
        object_schema(
            {
                "type": {"const": "http"},
                "url": {"type": "string", "minLength": 1},
                "headers": MAPPING,
                "env_headers": MAPPING,
                "bearer_token_env": TEXT,
            },
            ["type", "url"],
        ),
    ]
}
CHANGES = object_schema(
    {
        "transport": TRANSPORT,
        "enabled": {"type": "boolean"},
        "contexts": {
            "type": "array",
            "items": {"enum": ["foreground", "background"]},
            "maxItems": 2,
            "uniqueItems": True,
        },
        "allow_tools": nullable(STRINGS),
        "deny_tools": STRINGS,
        "approval": {"enum": ["approve", "reject"]},
        "tool_approvals": {
            "type": "object",
            "additionalProperties": {"enum": ["approve", "reject"]},
            "maxProperties": 100,
        },
        "startup_timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 3600},
        "tool_timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 3600},
    },
    description="Top-level field patch; transport replaces that whole object. Unsubmitted values are retained. New entries need transport. Secrets use private credential references.",
)
CHANGES["examples"] = [{"approval": "reject"}, {"enabled": False}]
