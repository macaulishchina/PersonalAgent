"""Owned test-command cleanup; separate process groups cannot escape Linux checks."""

import asyncio
import os
from pathlib import Path
import signal
import time


def process_tree(parent):
    """Remember descendant identities, including workers that create a new session."""
    processes = {}
    for path in Path("/proc").glob("[0-9]*/stat"):
        try:
            fields = path.read_text(encoding="utf-8").rsplit(")", 1)[1].split()
            processes[int(path.parent.name)] = (int(fields[1]), fields[19])
        except (OSError, ValueError, IndexError):
            continue
    owned, pending = {}, {parent}
    while pending:
        children = {
            pid: start for pid, (ppid, start) in processes.items() if ppid in pending and pid not in owned
        }
        owned.update(children)
        pending = set(children)
    return owned


def live_descendants(owned):
    result = {}
    for pid, start in owned.items():
        try:
            current = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").rsplit(")", 1)[1].split()
            if current[19] == start and current[0] not in {"Z", "X"}:
                result[pid] = start
        except (OSError, IndexError):
            pass
    return result


def signal_descendants(owned, action):
    for pid in live_descendants(owned):
        try:
            os.kill(pid, action)
        except ProcessLookupError:
            pass


async def terminate(process):
    # Each command owns a new process group, including worker/encoder children.
    descendants = process_tree(process.pid)
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    signal_descendants(descendants, signal.SIGTERM)
    try:
        await asyncio.wait_for(process.wait(), 2)
    except TimeoutError:
        pass
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        signal_descendants(descendants, signal.SIGKILL)
    await process.wait()
    deadline = time.monotonic() + 2
    while live_descendants(descendants):
        if time.monotonic() >= deadline:
            raise RuntimeError("Owned child processes did not exit after SIGKILL")
        await asyncio.sleep(0.01)
