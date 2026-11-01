"""Closed candidate contract; evidence context is deliberately extensible data."""

CONTEXT = {
    "type": "object",
    "additionalProperties": True,
    "description": "Evidence relevant to this judgment. Arbitrary non-secret JSON is allowed; privacy, depth and size rules are checked by the owner.",
}
OPTIONS = {
    "type": "object",
    "minProperties": 1,
    "maxProperties": 15,
    "propertyNames": {"type": "string", "pattern": "^[a-z][a-z0-9_]{0,63}$", "not": {"const": "uncertain"}},
    "additionalProperties": {"type": "string", "minLength": 1, "maxLength": 1000},
    "description": "Exact candidate IDs and descriptions; uncertain is reserved by the host.",
}
