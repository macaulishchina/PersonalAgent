"""Linux owned-process guardian. Pipe EOF survives supervisor SIGKILL."""

import ctypes
import os
from pathlib import Path
import selectors
import signal
import subprocess
import sys
import time


def descendants():
    rows = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdecimal():
            continue
        try:
            stat = (entry / "stat").read_text().rsplit(")", 1)[1].split()
            rows[int(entry.name)] = (int(stat[1]), stat[19])
        except (OSError, ValueError, IndexError):
            continue
    owned = {os.getpid()}
    while True:
        found = {pid for pid, (parent, _) in rows.items() if parent in owned}
        if found <= owned:
            return {pid: rows[pid][1] for pid in owned - {os.getpid()} if pid in rows}
        owned |= found


def send(pid, birth, sig):
    try:
        current = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
        if current == birth:
            os.kill(pid, sig)
    except (OSError, IndexError):
        pass


def main():
    # Orphaned Pi/tool subprocesses are reparented here, including separate sessions.
    if ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0):
        raise OSError("Cannot establish owned descendant cleanup")
    timeout = float(sys.argv[1])
    stop = False

    def terminate(*unused):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, terminate)
    signal.signal(signal.SIGINT, terminate)
    selector = selectors.DefaultSelector()
    selector.register(sys.stdin, selectors.EVENT_READ)
    env = {**os.environ, "PERSONAL_AGENT_SUPERVISOR_PID": str(os.getpid())}
    child = subprocess.Popen(sys.argv[2:], stdin=subprocess.DEVNULL, env=env)
    parent_lost = False
    while not stop and child.poll() is None:
        if selector.select(0.2) and not os.read(sys.stdin.fileno(), 1):
            parent_lost = True
            break
    if parent_lost or (not stop and child.poll() not in {None, 0}):
        from assistant_runtime.core.configuration.files import atomic
        import json

        root = Path(os.environ["PERSONAL_AGENT_INSTANCE_ROOT"])
        try:
            atomic(
                root / "state/configuration/unclean.json",
                json.dumps({"reason": "manager_lost" if parent_lost else "worker_failed"}).encode(),
            )
        except OSError:
            # A failed diagnostic write must never prevent descendant cleanup.
            print("Unclean shutdown marker unavailable; owned cleanup continues", file=sys.stderr)
    deadline = time.monotonic() + timeout
    known = {}
    while True:
        for pid, birth in descendants().items():
            if pid not in known:
                send(pid, birth, signal.SIGTERM)
                known[pid] = birth
        child.poll()
        # Reap adopted descendants as well as the direct worker.
        try:
            while os.waitpid(-1, os.WNOHANG)[0]:
                pass
        except ChildProcessError:
            break
        if time.monotonic() >= deadline:
            for pid, birth in descendants().items():
                send(pid, birth, signal.SIGKILL)
        time.sleep(0.05)
    selector.close()
    return child.returncode or 0


if __name__ == "__main__":
    sys.exit(main())
