"""Install one user unit. Does not alter host audio settings or start the service."""

import argparse
import os
from pathlib import Path
import sys


def unit_text(python, config, workspace):
    def quote(value):
        return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'

    return f"""[Unit]
Description=Personal Agent
StartLimitIntervalSec=0

[Service]
Type=simple
WorkingDirectory={str(workspace).replace("%", "%%")}
ExecStart={quote(python)} -m assistant_runtime.cli --config {quote(config)} run
Environment=PYTHONUNBUFFERED=1
Environment="PATH={os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")}"
Restart=on-failure
RestartSec=5
TimeoutStopSec=30
KillMode=control-group
UMask=0077

[Install]
WantedBy=default.target
"""


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--config", required=True)
    p.add_argument("--output", help="Render unit only; do not install or enable")
    a = p.parse_args()
    from assistant_runtime.config import Config

    path = Path(a.config).expanduser().resolve()
    config = Config.load(path)
    content = unit_text(sys.executable, path, config.workspace)
    unit = Path(a.output) if a.output else Path.home() / ".config/systemd/user" / config.unit_name
    unit.parent.mkdir(parents=True, exist_ok=True)
    unit.write_text(content)
    if not a.output:
        from assistant_runtime.cli import systemctl

        systemctl("daemon-reload")
        systemctl("enable", unit.name)
    print(unit)


if __name__ == "__main__":
    main()
