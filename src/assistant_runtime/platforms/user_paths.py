"""Per-user configuration locations, independent of shell activation."""

import os
from pathlib import Path
from assistant_runtime.api.system import system


def configuration_directory():
    if system.name == "windows":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData/Local")
    else:
        configured = os.environ.get("XDG_CONFIG_HOME", "")
        base = Path(configured) if configured and Path(configured).is_absolute() else Path.home() / ".config"
    return base / "personal-agent"
