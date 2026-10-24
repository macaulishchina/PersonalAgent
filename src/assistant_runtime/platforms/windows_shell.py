"""Own one Pi shell command's process tree without taskkill/WMI permissions."""

import asyncio
import base64
import json
import os
from pathlib import Path
import shutil
import sys


def command_line(shell, command):
    if shell == "powershell":
        executable = shutil.which("pwsh") or shutil.which("powershell")
        if not executable:
            executable = str(
                Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
            )
        command = (
            "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8\n"
            "$ProgressPreference = 'SilentlyContinue'\n" + command
        )
        encoded = base64.b64encode(command.encode("utf-16-le")).decode("ascii")
        return [
            executable,
            "-NoProfile",
            "-NonInteractive",
            "-OutputFormat",
            "Text",
            "-EncodedCommand",
            encoded,
        ]
    if shell == "bash":
        candidates = [
            Path(os.environ[key]) / "Git/bin/bash.exe"
            for key in ("ProgramFiles", "ProgramFiles(x86)")
            if key in os.environ
        ]
        executable = next((str(path) for path in candidates if path.is_file()), None) or shutil.which("bash")
        if not executable:
            raise RuntimeError("Install Git Bash or select PowerShell")
        windows = Path(os.environ["SystemRoot"])
        if Path(executable) in {windows / "System32/bash.exe", windows / "Sysnative/bash.exe"}:
            raise RuntimeError("WSL cannot be owned by a Windows Job; use Git Bash or PowerShell")
        return [executable, "-c", command]
    raise ValueError("Unsupported Windows shell")


async def run(request):
    import msvcrt
    import pywintypes
    import win32pipe
    from .common import Platform

    system = Platform()
    handle = msvcrt.get_osfhandle(sys.stdin.fileno())

    async def cancellation():
        # Node keeps stdin open until abort/timeout. EOF also covers owner death.
        # Polling avoids a blocking reader thread preventing normal shutdown.
        while True:
            try:
                win32pipe.PeekNamedPipe(handle, 0)
            except pywintypes.error as exc:
                if exc.winerror in {109, 232, 233}:
                    return
                raise
            await asyncio.sleep(0.02)

    process = await system.spawn(*command_line(request["shell"], request["command"]), stdin=-3)
    stopped = asyncio.create_task(cancellation())
    ended = asyncio.create_task(process.wait())
    try:
        done, _ = await asyncio.wait((stopped, ended), return_when=asyncio.FIRST_COMPLETED)
        if stopped in done:
            await stopped
            await system.stop_process(process)
            return 1
        return await ended
    finally:
        await system.stop_process(process)
        stopped.cancel()
        await asyncio.gather(stopped, ended, return_exceptions=True)


def main():
    if sys.platform != "win32":
        raise RuntimeError("Windows shell ownership is only available on Windows")
    request = json.loads(sys.stdin.buffer.readline())
    return asyncio.run(run(request))


if __name__ == "__main__":
    sys.exit(main())
