"""Explicit detached management processes and installed-service identity checks."""

import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import threading
from .executables import python_command


def installed(config, management):
    if os.name == "nt":
        import pywintypes
        import win32serviceutil
        from .services import name

        service = name(config.unit_name)
        try:
            win32serviceutil.QueryServiceStatus(service)
        except pywintypes.error as exc:
            if exc.winerror == 1060:
                return False
            raise
        deployment = json.loads(win32serviceutil.GetServiceCustomOption(service, "Deployment"))
        if Path(deployment["management"]).resolve() != Path(management).resolve():
            raise ValueError("Installed service belongs to another instance")
        return True
    unit = Path.home() / ".config/systemd/user" / config.unit_name
    if not unit.is_file():
        return False
    starts = [
        line.removeprefix("ExecStart=")
        for line in unit.read_text().splitlines()
        if line.startswith("ExecStart=")
    ]
    if len(starts) != 1:
        raise ValueError("Cannot verify installed service identity")
    args = shlex.split(starts[0].replace("%%", "%"))
    if (
        "--management" not in args
        or Path(args[args.index("--management") + 1]).resolve() != Path(management).resolve()
    ):
        raise ValueError("Installed service belongs to another instance")
    return True


def identity(pid):
    if os.name == "nt":
        import win32api
        import win32process

        process = win32api.OpenProcess(0x1000, False, pid)
        try:
            return str(win32process.GetProcessTimes(process)["CreationTime"])
        finally:
            process.Close()
    return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]


def running(record):
    try:
        return identity(record["pid"]) == record["identity"]
    except ProcessLookupError:
        return False
    except FileNotFoundError:
        return False
    except Exception as exc:
        if os.name == "nt" and getattr(exc, "winerror", None) == 87:
            return False  # PID no longer exists; permission errors are not treated as stopped.
        raise


def launch(management, log_path, owner_path):
    from assistant_runtime.api.system import system

    env = dict(os.environ)
    env.pop("PERSONAL_AGENT_SUPERVISOR_PID", None)
    env["PYTHONUNBUFFERED"] = "1"
    args, env = python_command(
        [
            sys.executable,
            "-m",
            "assistant_runtime.app.cli",
            "configuration",
            "serve",
            "--management",
            str(management),
        ],
        env,
    )
    options = (
        {"start_new_session": True}
        if os.name != "nt"
        else {
            "creationflags": subprocess.CREATE_NO_WINDOW
            | subprocess.CREATE_NEW_PROCESS_GROUP
            | subprocess.CREATE_BREAKAWAY_FROM_JOB
        }
    )
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("ab") as log:
        system.private(log_path)
        process = subprocess.Popen(
            args, stdin=subprocess.DEVNULL, stdout=log, stderr=log, env=env, close_fds=True, **options
        )
    try:
        record = {
            "pid": process.pid,
            "identity": identity(process.pid),
            "management": str(Path(management).resolve()),
        }
        system.atomic_write(owner_path, json.dumps(record).encode())
    except BaseException:
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=10)
        raise
    threading.Thread(target=process.wait, daemon=True, name="detached-manager-reaper").start()
    return process


def stop(owner_path, management):
    record = json.loads(owner_path.read_text())
    if record["management"] != str(Path(management).resolve()):
        raise ValueError("Detached manager identity changed; refusing to signal another process")
    if not running(record):
        return
    if os.name == "nt":
        import win32api
        import win32process

        process = win32api.OpenProcess(0x1001, False, record["pid"])
        try:
            if str(win32process.GetProcessTimes(process)["CreationTime"]) != record["identity"]:
                raise ValueError("Detached manager identity changed")
            win32api.TerminateProcess(process, 0)
        finally:
            process.Close()
    else:
        os.kill(record["pid"], signal.SIGTERM)
