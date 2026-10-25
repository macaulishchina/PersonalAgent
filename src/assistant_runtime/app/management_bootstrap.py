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
            from assistant_runtime.core.configuration.settings import ManagementSettings

            if ManagementSettings.load(found[0]).entry != entry:
                raise ValueError("Requested configuration does not match management entry")
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


def control_config(entry):
    """Resolve service identity without constructing any business configuration."""
    from types import SimpleNamespace
    import re
    from assistant_runtime.core.configuration.settings import ManagementSettings
    from .config import read_config

    settings = ManagementSettings.load(resolve_management(entry))
    try:
        raw = read_config(settings.entry)
    except (ValueError, OSError):
        # The accepted snapshot, unlike the editable file, survives a broken edit.
        saved = settings.root / "state/configuration/loaded-snapshot.json"
        raw = {}
        if saved.exists():
            import tomllib
            from assistant_runtime.core.configuration.fields import merge

            import base64

            snapshot = json.loads(saved.read_text(encoding="utf-8"))
            for content in snapshot["contents"].values():
                merge(raw, tomllib.loads(base64.b64decode(content, validate=True).decode()))
    unit = raw.get("unit_name", "personal-agent.service")
    if not isinstance(unit, str) or not re.fullmatch(r"[A-Za-z0-9_.@-]+", unit):
        raise ValueError("Invalid service identity; repair unit_name locally")
    return SimpleNamespace(
        root=settings.root,
        state_dir=settings.root / "state",
        unit_name=unit,
        socket=settings.root / "state/control.sock",
        model_api={},
    )
