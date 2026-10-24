"""Install the pinned Pi runtime; usable on Linux and native Windows."""

import os
from pathlib import Path
import shutil
import subprocess

from assistant_runtime.platforms.pi_installation import default_directory, remember


def setup(destination=None):
    destination = (
        Path(destination).expanduser() if destination is not None else default_directory()
    ).resolve()
    import sys

    source = Path(sys.prefix) / "share/personal-agent/pi"
    if not source.is_dir():
        source = Path(__file__).resolve().parents[3] / "deploy/pi"
    if not source.is_dir():
        raise RuntimeError("Pinned Pi installation resources are missing from this package")
    node, npm = shutil.which("node"), shutil.which("npm")
    if not node or not npm:
        raise RuntimeError("Install Node and npm before installing Pi")
    npm_path = Path(npm).resolve()
    # Invoke npm's JS entry directly on Windows, avoiding cmd.exe argument parsing.
    npm_cli = npm_path.parent / "node_modules/npm/bin/npm-cli.js"
    command = [node, str(npm_cli)] if os.name == "nt" else [npm]
    if os.name == "nt" and not npm_cli.is_file():
        raise RuntimeError("Cannot locate npm-cli.js beside the Node installation")
    destination.mkdir(parents=True, exist_ok=True)
    for filename in ("package.json", "package-lock.json"):
        shutil.copyfile(source / filename, destination / filename)
    subprocess.run([*command, "--prefix", str(destination), "ci", "--no-audit", "--no-fund"], check=True)
    binary = destination / "node_modules/.bin" / ("pi.cmd" if os.name == "nt" else "pi")
    if not binary.is_file():
        raise RuntimeError("Pi installation did not produce its executable; installation was not recorded")
    remember(binary, node)
    return binary
