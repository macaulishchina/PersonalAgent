"""Per-user pinned Pi installation, independent of an interactive shell's PATH."""

import json
import os
from pathlib import Path

from assistant_runtime.api.system import system


def data_directory():
    if system.name == "windows":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData/Local")
    else:
        configured = os.environ.get("XDG_DATA_HOME", "")
        base = (
            Path(configured)
            if configured and Path(configured).is_absolute()
            else Path.home() / ".local/share"
        )
    return base / "personal-agent"


def default_directory():
    return data_directory() / "pi"


def remember(binary, node):
    directory = data_directory()
    directory.mkdir(parents=True, exist_ok=True)
    system.private(directory, directory=True)
    system.atomic_write(
        directory / "pi-installation.json",
        json.dumps({"binary": str(Path(binary).resolve()), "node": str(Path(node).resolve())}).encode(),
    )


def installed():
    """An explicit recorded installation must never silently fall back to another Pi."""
    record = data_directory() / "pi-installation.json"
    try:
        data = json.loads(record.read_text(encoding="utf-8"))
    except FileNotFoundError:
        binary = default_directory() / "node_modules/.bin" / ("pi.cmd" if system.name == "windows" else "pi")
        return (binary, None) if binary.is_file() else None
    except (OSError, ValueError) as exc:
        raise RuntimeError("Cannot read Pi installation record; run install-pi again") from exc
    if (
        not isinstance(data, dict)
        or set(data) != {"binary", "node"}
        or any(not isinstance(data[key], str) or not Path(data[key]).is_absolute() for key in data)
    ):
        raise RuntimeError("Invalid Pi installation record; run install-pi again")
    binary, node = Path(data["binary"]), Path(data["node"])
    if not binary.is_file() or not node.is_file():
        raise RuntimeError("Recorded Pi or Node installation is missing; run install-pi again")
    return binary, node
