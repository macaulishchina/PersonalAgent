"""Explicit synthetic release metadata for extension contract tests."""


def release_data(name="sample", **changes):
    return {
        "schema_version": 1,
        "id": name,
        "version": "1.0.0",
        "api_version": 2,
        "requires_core": ">=0.4.0,<0.5.0",
        "requires": {},
        "optional": {},
        "critical": False,
        "supported_platforms": ["linux", "windows"],
        **changes,
    }
