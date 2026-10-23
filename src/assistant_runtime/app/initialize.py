"""Create a private instance without model requests or service changes."""

from pathlib import Path
import shutil


def initialize(destination, template=None, *, preset="standard"):
    if preset not in {"slim", "standard"}:
        raise ValueError("Unknown instance preset")
    destination = Path(destination).expanduser().resolve()
    template = Path(template).resolve() if template else Path(__file__).with_name("templates") / preset
    if destination.exists():
        raise ValueError("Instance destination already exists")
    destination.mkdir(mode=0o700, parents=True)
    try:
        shutil.copytree(template, destination / "config")
        (destination / "config").chmod(0o700)
        for path in (destination / "config").rglob("*"):
            path.chmod(0o700 if path.is_dir() else 0o600)
        (destination / "state").mkdir(mode=0o700)
        (destination / ".env").touch(mode=0o600)
        import json
        from assistant_runtime.core.configuration.auth import AdminAccess

        management = destination / "management.toml"
        management.write_text(
            "root = " + json.dumps(str(destination)) + "\n"
            "entry = " + json.dumps(str(destination / "config/config.toml")) + "\n"
            'host = "0.0.0.0"\nport = 8047\n'
        )
        management.chmod(0o600)
        private = destination / "state/configuration"
        private.mkdir(mode=0o700)
        AdminAccess(private / "admin-token")
    except BaseException:
        shutil.rmtree(destination)
        raise
    return destination / "config/config.toml"
