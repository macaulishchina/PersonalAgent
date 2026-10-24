"""Service dispatch. Service managers never own business configuration policy."""

import os
from pathlib import Path
import subprocess


def linux_systemctl(*args):
    env = os.environ.copy()
    env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    env.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path={env['XDG_RUNTIME_DIR']}/bus")
    return subprocess.run(["systemctl", "--user", *args], env=env, check=True)


def name(unit):
    return unit.removesuffix(".service")


def control(action, config):
    owner = Path(config.state_dir) / "configuration/detached-manager.json"
    if owner.is_file() and action in {"stop", "restart", "status", "logs"}:
        import json
        from .daemon import installed, running, launch, stop

        record = json.loads(owner.read_text())
        management = Path(record["management"])
        if not installed(config, management):
            if action == "logs":
                print((owner.parent / "manager.log").read_text(encoding="utf-8", errors="replace")[-65536:])
                return
            if action == "status":
                active = running(record)
                return {"state": "running" if active else "stopped", "pid": record["pid"]}
            stop(owner, management)
            if action == "restart":
                import time
                from assistant_runtime.api.system import system

                deadline = time.monotonic() + 75
                while True:
                    try:
                        with (
                            (owner.parent / "manager.lock").open("a+b") as lock,
                            (Path(config.state_dir) / "service.lock").open("a+b") as worker,
                        ):
                            system.lock(lock, blocking=False)
                            system.lock(worker, blocking=False)
                        break
                    except BlockingIOError:
                        if time.monotonic() >= deadline:
                            raise TimeoutError("Previous instance has not stopped")
                        time.sleep(0.1)
                launch(management, owner.parent / "manager.log", owner)
            return
    if os.name != "nt":
        if action == "logs":
            return subprocess.run(
                ["journalctl", "--user", "-u", config.unit_name, "-n", "60", "--no-pager"],
                check=True,
            )
        if action == "uninstall":
            result = subprocess.run(["systemctl", "--user", "is-active", "--quiet", config.unit_name])
            if result.returncode == 0:
                raise ValueError("Stop the service before uninstalling")
            linux_systemctl("disable", config.unit_name)
            (Path.home() / ".config/systemd/user" / config.unit_name).unlink()
            return linux_systemctl("daemon-reload")
        return linux_systemctl(action, config.unit_name)
    import win32service
    import win32serviceutil

    service = name(config.unit_name)
    if action == "logs":
        path = Path(config.state_dir) / "configuration/business.log"
        if path.exists():
            with path.open("rb") as stream:
                stream.seek(max(0, path.stat().st_size - 65536))
                print(stream.read().decode("utf-8", errors="replace"))
        return
    if action == "start":
        return win32serviceutil.StartService(service)
    if action in {"stop", "restart"}:
        import json
        from assistant_runtime.core.configuration.settings import ManagementSettings

        deployment = json.loads(win32serviceutil.GetServiceCustomOption(service, "Deployment"))
        timeout = ManagementSettings.load(deployment["management"]).shutdown_seconds + 15
        win32serviceutil.StopService(service)
        win32serviceutil.WaitForServiceStatus(service, win32service.SERVICE_STOPPED, timeout)
        if action == "restart":
            return win32serviceutil.StartService(service)
        return
    if action == "status":
        return win32serviceutil.QueryServiceStatus(service)
    if action == "uninstall":
        if win32serviceutil.QueryServiceStatus(service)[1] != win32service.SERVICE_STOPPED:
            raise ValueError("Stop the service before uninstalling")
        return win32serviceutil.RemoveService(service)
    raise ValueError("Unsupported service operation")
