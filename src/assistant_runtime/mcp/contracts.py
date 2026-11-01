"""Shared first-party schema adapter; business schemas stay with their owners.

External Python plugins continue to declare public Tool schemas. Third-party MCP
protocols are not rewritten or given guessed business parameters by this module.
"""

from functools import partial
from pydantic import BeforeValidator
from jsonschema import Draft202012Validator
from assistant_runtime.api.tool_calls import invalid_arguments


def object_schema(properties, required=(), **constraints):
    return {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
        **({"required": list(required)} if required else {}),
        **constraints,
    }


def nullable(schema):
    return {"anyOf": [schema, {"type": "null"}]}


def validate(value, schema, prefix):
    errors = sorted(Draft202012Validator(schema).iter_errors(value), key=lambda e: str(list(e.path)))
    if errors:
        error = errors[0]
        parts = [*([prefix] if prefix else []), *map(str, error.path)]
        if error.validator == "required" and isinstance(error.instance, dict):
            missing = [name for name in error.validator_value if name not in error.instance]
            if missing:
                parts.append(missing[0])
        path = ".".join(parts)
        # Validator messages can quote secret values. Expose constraints and field names only.
        if error.validator == "additionalProperties" and isinstance(error.instance, dict):
            fields = sorted(set(error.instance) - set(error.schema.get("properties", {})))
            message = "Unknown fields: " + ", ".join(fields)
        elif error.validator == "required":
            message = "Required field is missing"
        elif error.validator == "type":
            message = "Expected type " + str(error.validator_value)
        elif error.validator == "enum":
            message = "Choose one of " + str(error.validator_value)
        else:
            message = (
                error.schema.get("description")
                or "Invalid field; expected " + str(error.validator) + " constraint"
            )
        raise invalid_arguments(message + " at " + path, path)

    return value


def checked(schema, prefix):
    """Pydantic and runtime share the same owner schema, not schema-only annotations."""
    return BeforeValidator(partial(validate, schema=schema, prefix=prefix))
