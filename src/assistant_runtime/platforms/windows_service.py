"""SCM host for the same management process; requires an explicit ordinary account."""

import asyncio
from pathlib import Path
import sys

import win32event
import win32service
import win32serviceutil


class AgentService(win32serviceutil.ServiceFramework):
    _svc_name_ = "personal-agent"
    _svc_display_name_ = "Personal Agent"

    def __init__(self, args):
        super().__init__(args)
        self.service_name = args[0]
        self.stop_handle = win32event.CreateEvent(None, True, False, None)
        self.shutdown_seconds = 60

    def SvcStop(self):
        self.ReportServiceStatus(
            win32service.SERVICE_STOP_PENDING, waitHint=(self.shutdown_seconds + 15) * 1000
        )
        win32event.SetEvent(self.stop_handle)

    def SvcShutdown(self):
        self.SvcStop()

    def SvcDoRun(self):
        import os
        import json
        from assistant_runtime.app.management import serve
        from assistant_runtime.core.configuration.settings import ManagementSettings

        deployment = json.loads(win32serviceutil.GetServiceCustomOption(self.service_name, "Deployment"))
        os.environ["PATH"] = deployment["path"]
        os.environ["PYTHONUTF8"] = "1"
        os.chdir(deployment["working_directory"])
        # All children use the instance interpreter even under SCM.
        sys.executable = deployment["python"]
        settings = ManagementSettings.load(deployment["management"])
        self.shutdown_seconds = settings.shutdown_seconds

        async def run():
            stop = asyncio.Event()

            async def watch():
                await asyncio.to_thread(win32event.WaitForSingleObject, self.stop_handle, win32event.INFINITE)
                stop.set()

            watcher = asyncio.create_task(watch())
            try:
                await serve(settings, stop=stop)
            finally:
                win32event.SetEvent(self.stop_handle)
                await watcher

        asyncio.run(run())


def install(config, management, account, password):
    import json
    import os
    import shutil
    import subprocess
    import win32api
    import win32con
    import win32security
    import pywintypes
    from .services import name

    if not account or account.casefold() in {"localsystem", "nt authority\\system"}:
        raise ValueError("An explicit ordinary service account is required")
    sid = win32security.LookupAccountName(None, account)[0]
    token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
    try:
        current = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
    finally:
        token.Close()
    if sid != current:
        raise ValueError(
            "Run installation as the chosen instance owner; do not copy another account's credentials"
        )
    domain, separator, user = account.rpartition("\\")
    try:
        login = win32security.LogonUser(
            user if separator else account,
            domain if separator else None,
            password,
            win32con.LOGON32_LOGON_SERVICE,
            win32con.LOGON32_PROVIDER_DEFAULT,
        )
    except pywintypes.error as exc:
        raise ValueError(
            f"Service account logon preflight failed (Windows error {exc.winerror}); "
            "check the password and 'Log on as a service' policy before installing"
        ) from None
    else:
        login.Close()
    node = shutil.which("node")
    if not node:
        raise ValueError("Node is not installed for the service account")
    interpreter = Path(sys.executable).resolve()
    if not interpreter.is_file() or not Path(management).is_file():
        raise ValueError("Service interpreter or management configuration missing")
    service = name(config.unit_name)
    # No password is persisted by the application. SCM stores the account secret.
    win32serviceutil.InstallService(
        "assistant_runtime.platforms.windows_service.AgentService",
        service,
        "Personal Agent",
        startType=win32service.SERVICE_AUTO_START,
        userName=account,
        password=password,
        exeName=str(interpreter),
        exeArgs=subprocess.list2cmdline(
            ["-m", "assistant_runtime.platforms.windows_service", "--service", service]
        ),
        description="Personal Agent configuration manager and owned business worker",
    )
    try:
        win32serviceutil.SetServiceCustomOption(
            service,
            "Deployment",
            json.dumps(
                {
                    "python": str(interpreter),
                    "management": str(Path(management).resolve()),
                    "working_directory": str(config.workspace),
                    "path": os.pathsep.join(
                        dict.fromkeys(
                            [
                                str(interpreter.parent),
                                str(Path(node).parent),
                                str(Path(os.environ["SystemRoot"]) / "System32"),
                                str(Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0"),
                            ]
                        )
                    ),
                }
            ),
        )
    except BaseException:
        win32serviceutil.RemoveService(service)
        raise
    return service


if __name__ == "__main__":
    import servicemanager

    if len(sys.argv) != 3 or sys.argv[1] != "--service":
        raise SystemExit("Use pa service install")
    AgentService._svc_name_ = sys.argv[2]
    servicemanager.Initialize()
    servicemanager.PrepareToHostSingle(AgentService)
    servicemanager.StartServiceCtrlDispatcher()
