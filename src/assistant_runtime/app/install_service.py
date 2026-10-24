"""Install one user unit. Does not alter host audio settings or start the service."""

import argparse
import shutil
from pathlib import Path
import sys


def unit_text(python, config, workspace, management=None, shutdown_seconds=60):
    def quote(value):
        return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'

    node = shutil.which("node")
    path = ":".join(
        dict.fromkeys(
            [
                str(Path(python).absolute().parent),
                *([str(Path(node).resolve().parent)] if node else []),
                str(Path.home() / ".local/bin"),
                "/usr/local/bin",
                "/usr/bin",
                "/bin",
            ]
        )
    )
    return f"""[Unit]
Description=Personal Agent
StartLimitIntervalSec=300
StartLimitBurst=3

[Service]
Type=simple
WorkingDirectory={str(workspace).replace("%", "%%")}
ExecStart={quote(python)} -m assistant_runtime.app.cli configuration serve --management {quote(management or Path(config).parent.parent / "management.toml")}
Environment=PYTHONUNBUFFERED=1
Environment="PATH={path}"
Restart=on-failure
RestartSec=5
TimeoutStopSec={shutdown_seconds + 15}
KillMode=control-group
UMask=0077

[Install]
WantedBy=default.target
"""


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--config", required=True)
    p.add_argument("--output", help="Render unit only; do not install or enable")
    p.add_argument("--account", help="Windows service account (explicit instance owner)")
    a = p.parse_args(argv)
    from assistant_runtime.app.config import Config

    path = Path(a.config).expanduser().resolve()
    config = Config.load(path)
    from assistant_runtime.app.management_bootstrap import ensure_management
    from assistant_runtime.core.configuration.settings import ManagementSettings

    management = ensure_management(config.root, path)
    settings = ManagementSettings.load(management)
    import os

    if os.name == "nt":
        if a.output:
            raise ValueError("Windows service installation uses SCM, not a unit file")
        if not a.account:
            p.error("Windows service installation requires --account")
        from getpass import getpass
        from assistant_runtime.platforms.windows_service import install

        print(install(config, management, a.account, getpass("Service account password: ")))
        return
    content = unit_text(sys.executable, path, config.workspace, management, settings.shutdown_seconds)
    unit = Path(a.output) if a.output else Path.home() / ".config/systemd/user" / config.unit_name
    unit.parent.mkdir(parents=True, exist_ok=True)
    unit.write_text(content, encoding="utf-8")
    if not a.output:
        from assistant_runtime.app.cli import systemctl

        systemctl("daemon-reload")
        systemctl("enable", unit.name)
    print(unit)


if __name__ == "__main__":
    main()
