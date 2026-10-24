"""Resolve executable vectors explicitly; never pass an npm .cmd shim to a shell."""

import json
import os
from pathlib import Path
import shutil
import sys


def python_command(args, env):
    """Bypass the Windows venv redirector while retaining its site-packages.

    Like multiprocessing.popen_spawn_win32, this keeps the reported child PID
    and its actual parent relationship intact for supervision and cancellation.
    """
    args = list(args)
    if (
        os.name == "nt"
        and args
        and os.path.normcase(os.path.abspath(args[0])) == os.path.normcase(sys.executable)
        and sys.prefix != sys.base_prefix
    ):
        env = dict(os.environ if env is None else env)
        env["__PYVENV_LAUNCHER__"] = sys.executable
        args[0] = getattr(sys, "_base_executable", sys.executable)
    return args, env


def pi_command(binary):
    executable = shutil.which(binary)
    if not executable:
        raise RuntimeError("Pi executable not found; run scripts/setup_pi.py and configure pi_binary")
    path = Path(executable).resolve()
    if os.name != "nt" or path.suffix.lower() not in {".cmd", ".bat", ".ps1"}:
        return [str(path)]
    candidates = [
        path.parent.parent / "@earendil-works/pi-coding-agent",
        path.parent / "node_modules/@earendil-works/pi-coding-agent",
    ]
    node = shutil.which("node")
    for package in candidates:
        manifest = package / "package.json"
        if not manifest.is_file():
            continue
        entry = (package / json.loads(manifest.read_text(encoding="utf-8"))["bin"]["pi"]).resolve()
        if node and entry.is_relative_to(package.resolve()) and entry.is_file():
            return [node, str(entry)]
    raise RuntimeError("Cannot resolve the installed Pi npm entry; install the pinned runtime")


def command_tools(shell="auto"):
    if shell not in {"auto", "bash", "powershell"}:
        raise ValueError("pi_shell must be auto, bash or powershell")
    return "powershell" if shell == "powershell" or (shell == "auto" and os.name == "nt") else "bash"


def executable_command(args):
    if os.name != "nt" or not args:
        return list(args)
    executable = shutil.which(str(args[0])) or str(args[0])
    path = Path(executable)
    if path.suffix.lower() not in {".cmd", ".bat", ".ps1"}:
        return [executable, *args[1:]]
    if path.stem.casefold() == "pi":
        return [*pi_command(executable), *args[1:]]
    if path.stem.casefold() in {"npm", "npx"}:
        cli = path.parent / "node_modules/npm/bin" / (path.stem.casefold() + "-cli.js")
        node = shutil.which("node")
        if node and cli.is_file():
            return [node, str(cli), *args[1:]]
    raise ValueError("Configure an explicit executable and script arguments instead of a shell launcher")
