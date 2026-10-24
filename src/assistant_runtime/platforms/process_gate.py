"""Windows child launcher. Wait for ownership before executing any target code."""

import subprocess
import sys
import os


def main():
    import win32con
    import win32event

    gate = win32event.OpenEvent(win32con.SYNCHRONIZE, False, sys.argv[1])
    try:
        if win32event.WaitForSingleObject(gate, 30000) != win32event.WAIT_OBJECT_0:
            raise RuntimeError("Process ownership was not established")
    finally:
        gate.Close()
    env = os.environ.copy()
    if env.pop("PERSONAL_AGENT_OWNED_BUSINESS", None) == "1":
        env["PERSONAL_AGENT_SUPERVISOR_PID"] = str(os.getpid())
    return subprocess.call(sys.argv[2:], env=env)


if __name__ == "__main__":
    sys.exit(main())
