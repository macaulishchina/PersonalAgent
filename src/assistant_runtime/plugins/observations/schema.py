"""Small metadata helper; no protocol catalogue or device branches."""


def schema(properties, types, required=()):
    return {
        "config_schema": {
            "type": "object",
            "properties": properties,
            "required": list(required),
            "additionalProperties": False,
        },
        "types": types,
        "event_versions": {kind: 1 for kind in types},
        "verification": "Read-only connectivity; transition acceptance is separate",
    }


def fields(config, allowed):
    if not isinstance(config, dict) or set(config) - set(allowed):
        raise ValueError("Unknown observation configuration field")


def interval(config, default, minimum):
    value = config.get("interval", default)
    if type(value) not in (int, float) or not minimum <= value <= 3600:
        raise ValueError("Invalid source polling interval")
    return value
