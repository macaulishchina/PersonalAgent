"""Resolve private provider credentials."""

from assistant_runtime.api.configuration_io import configuration_path

import os


def credential(root, key):
    from dotenv import dotenv_values

    value = os.environ.get(key) or dotenv_values(configuration_path(root / ".env"), interpolate=False).get(
        key
    )
    if not value or not isinstance(value, str) or "\n" in value or "\r" in value:
        raise ValueError("Missing or invalid model credential: " + key)
    return value
