"""Resolve independent bootstrap before reading possibly broken business configuration."""

import json
from pathlib import Path
from assistant_runtime.core.configuration.files import atomic


def ensure_management(root, entry):
    path = Path(root) / "management.toml"
    if not path.exists():
        atomic(
            path,
            (
                "root = "
                + json.dumps(str(root))
                + "\nentry = "
                + json.dumps(str(entry))
                + '\nhost = "0.0.0.0"\nport = 8047\n'
            ).encode(),
        )
    return path


def resolve_management(entry):
    if entry:
        entry = Path(entry).expanduser().resolve()
        options = [entry.parent / "management.toml", entry.parent.parent / "management.toml"]
        found = [p for p in options if p.is_file()]
        if len(found) == 1:
            return found[0]
        if len(found) > 1:
            raise ValueError("Ambiguous management bootstrap; use configuration serve --management")
    from .config import Config

    config = Config.load(entry)
    if entry is None:
        entry = config.root / "config/config.toml"
        if not entry.exists():
            raise ValueError("Initialize an instance first")
    return ensure_management(config.root, entry)
