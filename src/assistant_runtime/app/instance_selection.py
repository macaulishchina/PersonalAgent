"""Select one existing instance; never create, migrate or start it implicitly."""

import json
import os
from pathlib import Path
from assistant_runtime.api.system import system
from assistant_runtime.platforms.user_paths import configuration_directory


def entry_path(value):
    path = Path(value).expanduser().resolve()
    if path.is_dir():
        management = path / "management.toml"
        if management.is_file():
            from assistant_runtime.core.configuration.settings import ManagementSettings

            entry = ManagementSettings.load(management).entry
        else:
            candidates = [p for p in (path / "config/config.toml", path / "config.toml") if p.is_file()]
            if len(candidates) != 1:
                raise ValueError("Select an instance directory with one entry, or an explicit config file")
            entry = candidates[0]
    else:
        entry = path
    if not entry.is_file():
        raise ValueError(f"Instance configuration does not exist: {entry}; use pa use <instance-directory>")
    return entry.resolve()


def select(value):
    entry = entry_path(value)
    # Selection remains usable when a business setting needs repair. Starting still
    # runs Config.load and all certification/model validation before any plugin opens.
    from .config import read_config

    read_config(entry)
    folder = configuration_directory()
    folder.mkdir(parents=True, exist_ok=True)
    system.private(folder, directory=True)
    system.atomic_write(folder / "cli.json", json.dumps({"config": str(entry)}).encode())
    return entry


def resolve(explicit=None, *, required=True):
    chosen = explicit or os.environ.get("PERSONAL_AGENT_CONFIG")
    if chosen:
        return entry_path(chosen)
    saved = configuration_directory() / "cli.json"
    if saved.exists():
        try:
            data = json.loads(saved.read_text(encoding="utf-8"))
            if (
                not isinstance(data, dict)
                or set(data) != {"config"}
                or not isinstance(data["config"], str)
                or not Path(data["config"]).is_absolute()
            ):
                raise ValueError("Invalid selection")
        except (OSError, ValueError) as exc:
            raise ValueError(
                "Cannot read the selected instance; run pa use <instance-directory> again"
            ) from exc
        return entry_path(data["config"])
    cwd = Path.cwd()
    if any(
        (cwd / relative).is_file() for relative in ("management.toml", "config/config.toml", "config.toml")
    ):
        return entry_path(cwd)
    legacy = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "personal-agent/config.toml"
    if legacy.is_file():
        return entry_path(legacy)
    if required:
        raise ValueError(
            "No instance selected. Run pa use <existing-instance-directory> once, then pa. To create one explicitly: pa init <directory>."
        )
    return None
