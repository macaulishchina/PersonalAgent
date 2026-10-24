"""Create a private instance without model requests or service changes."""

from assistant_runtime.api.system import system

from pathlib import Path
import shutil
from .presets import default_preset


def initialize(destination, template=None, *, preset=None):
    preset = preset or default_preset()
    if preset not in {"slim", "standard"}:
        raise ValueError("Unknown instance preset")
    if preset == "standard" and system.name == "windows":
        raise ValueError(
            "standard does not support windows: web, voice_disabled are certified for linux only; "
            "use --preset slim"
        )
    destination = Path(destination).expanduser().resolve()
    template = Path(template).resolve() if template else Path(__file__).with_name("templates") / preset
    if destination.exists():
        raise ValueError("Instance destination already exists")
    from assistant_runtime.app.config import _read_config
    from assistant_runtime.core.plugin_catalog import validate_platforms

    selected = _read_config(template / "config.toml").get("plugins", [])
    validate_platforms(selected, system.name)
    destination.mkdir(mode=0o700, parents=True)
    system.private(destination, directory=True)
    try:
        shutil.copytree(template, destination / "config")
        system.private(destination / "config", directory=True)
        for path in (destination / "config").rglob("*"):
            system.private(path, directory=path.is_dir())
        (destination / "state").mkdir(mode=0o700)
        system.private(destination / "state", directory=True)
        (destination / ".env").touch(mode=0o600)
        import json
        from assistant_runtime.core.configuration.auth import AdminAccess

        management = destination / "management.toml"
        management.write_text(
            "root = " + json.dumps(str(destination)) + "\n"
            "entry = " + json.dumps(str(destination / "config/config.toml")) + "\n"
            'host = "0.0.0.0"\nport = 8047\n',
            encoding="utf-8",
        )
        system.private(management)
        private = destination / "state/configuration"
        private.mkdir(mode=0o700)
        AdminAccess(private / "admin-token")
    except BaseException:
        shutil.rmtree(destination)
        raise
    return destination / "config/config.toml"
