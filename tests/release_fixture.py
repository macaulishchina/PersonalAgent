"""Explicit synthetic release metadata for extension contract tests."""


def release_data(name="sample", **changes):
    return {
        "schema_version": 1,
        "id": name,
        "version": "1.0.0",
        "api_version": 2,
        "requires_core": ">=0.5.1,<0.6.0",
        "requires": {},
        "optional": {},
        "critical": False,
        "supported_platforms": ["linux", "windows"],
        **changes,
    }
