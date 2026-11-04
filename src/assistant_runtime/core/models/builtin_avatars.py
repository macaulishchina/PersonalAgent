"""Versioned, packaged SVG avatars; no network or instance configuration access."""

import base64
from functools import lru_cache
import hashlib
from importlib.resources import files
import json


@lru_cache(maxsize=1)
def _manifest():
    raw = files(__package__).joinpath("avatars/catalog.json").read_bytes()
    return hashlib.sha256(raw).hexdigest(), json.loads(raw)["avatars"]


def names():
    return tuple(item["name"] for item in _manifest()[1])


def catalog():
    revision, items = _manifest()
    return {
        "revision": revision,
        "avatars": [
            {
                "name": item["name"],
                "label": item["label"],
                "group": item["group"],
                "version": item["sha256"],
                "media_type": "image/svg+xml",
            }
            for item in items
        ],
    }


def read(name, version=None):
    item = next((item for item in _manifest()[1] if item["name"] == name), None)
    if item is None or version is not None and version != item["sha256"]:
        raise ValueError("Unknown built-in avatar or stale version")
    raw = files(__package__).joinpath("avatars", item["name"] + ".svg").read_bytes()
    if len(raw) > 128 * 1024 or hashlib.sha256(raw).hexdigest() != item["sha256"]:
        raise ValueError("Invalid packaged avatar")
    return raw, item["sha256"]


def encoded(name, version=None):
    raw, version = read(name, version)
    return {
        "asset_id": "builtin:" + name,
        "version": version,
        "media_type": "image/svg+xml",
        "sha256": version,
        "data_base64": base64.b64encode(raw).decode(),
    }
