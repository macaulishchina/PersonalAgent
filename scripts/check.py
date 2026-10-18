"""Scoped development checks; no arguments run the full pre-commit gate.

Tests run in separate processes per module, never concurrent threads sharing mocks,
environment variables or event loops. This script never restarts the installed service.
"""

import argparse
import asyncio
from dataclasses import dataclass
import os
from pathlib import Path
import re
import signal
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
GROUPS = {
    "core": {"behavior", "conversation", "quiet", "runtime", "voice_binding", "wake_words"},
    "codex": {"codex_config", "integrations", "job_lifecycle", "mcp", "native", "voice_lifecycle"},
    "devices": {
        "audio_input_quality",
        "audio_input_guide",
        "audio_outputs",
        "camera_motion",
        "device_operations",
        "devices",
        "environment",
        "go2rtc",
        "video",
        "video_recording",
    },
    "events": {"events", "event_sources", "event_reporting"},
    "decisions": {"decisions", "decisions_transport"},
    "tasks": {"notifications", "tasks", "task_events", "task_models", "task_self_schedule"},
    "media": {"media", "media_providers", "media_downloads"},
    "memory": {"mem0", "memory"},
    "web": {"reply_images", "reply_videos", "web"},
}
SLOW_FIRST = ("test_tasks", "test_mem0", "test_environment")


def modules(root):
    directory = root / "tests"
    return {
        ".".join(path.relative_to(directory).with_suffix("").parts) for path in directory.rglob("test_*.py")
    }


def module_for(target, available):
    return next(
        (
            name
            for name in sorted(available, key=len, reverse=True)
            if target == name or target.startswith(name + ".")
        ),
        None,
    )


def groups(root):
    available = modules(root)
    result = {name: {"test_" + item for item in entries} & available for name, entries in GROUPS.items()}
    # New modules always run in full checks, even before being assigned a group.
    result["core"] |= available - set().union(*result.values())
    return result


def select(root, selected_groups=(), targets=()):
    available, grouped = modules(root), groups(root)
    if not available:
        raise ValueError("No test modules discovered")
    names = set().union(*(grouped[name] for name in selected_groups)) if selected_groups else set()
    if not selected_groups and not targets:
        names = available
    for target in targets:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*", target):
            raise ValueError("Use a unittest module, class or method, e.g. test_integrations.AdapterTest")
        module = module_for(target, available)
        if module is None or len(target.removeprefix(module).split(".")) > 3:
            raise ValueError("Unknown test target: " + target)
        names.add(target)
    names = {name for name in names if not any(name.startswith(other + ".") for other in names)}
    result = {}
    for name in sorted(names):
        result.setdefault(module_for(name, available), []).append(name)
    if not result:
        raise ValueError("No tests selected")
    return result


@dataclass
class Outcome:
    label: str
    code: int
    elapsed: float
    output: str

    @property
    def count(self):
        match = re.search(r"Ran (\d+) tests? in", self.output)
        return int(match[1]) if match else 0

    @property
    def skipped(self):
        match = re.search(r"skipped=(\d+)", self.output)
        return int(match[1]) if match else 0


def process_tree(parent):
    """Remember descendant identities, including workers that create a new session."""
    processes = {}
    for path in Path("/proc").glob("[0-9]*/stat"):
        try:
            fields = path.read_text().rsplit(")", 1)[1].split()
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
            current = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
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


async def execute(label, args, root, timeout, env=None):
    started = time.monotonic()
    process = await asyncio.create_subprocess_exec(
        *args,
        cwd=root,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        start_new_session=True,
    )
    try:
        output, _ = await asyncio.wait_for(process.communicate(), timeout)
    except TimeoutError:
        await terminate(process)
        return Outcome(
            label, 124, time.monotonic() - started, f"Timed out after {timeout}s; owned processes stopped"
        )
    except BaseException:
        await terminate(process)
        raise
    return Outcome(label, process.returncode, time.monotonic() - started, output.decode(errors="replace"))


def display(result, verbose=False, *, tests=False):
    details = f" · {result.count} tests, {result.skipped} skipped" if tests else ""
    print(
        f"{'PASS' if result.code == 0 else 'FAIL'} {result.label} · {result.elapsed:.2f}s{details}",
        flush=True,
    )
    if result.code or verbose:
        print(result.output.rstrip(), flush=True)


async def run_tests(root, selection, jobs, timeout, verbose=False):
    semaphore = asyncio.Semaphore(jobs)
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(filter(None, (str(root / "tests"), env.get("PYTHONPATH"))))

    async def run(module, targets):
        async with semaphore:
            result = await execute(
                module,
                [sys.executable, "-m", "unittest", "-v", "--durations", "5", *targets],
                root,
                timeout,
                env,
            )
            display(result, verbose, tests=True)
            return result

    ordered = sorted(
        selection, key=lambda name: (SLOW_FIRST.index(name) if name in SLOW_FIRST else len(SLOW_FIRST), name)
    )
    pending = [asyncio.create_task(run(module, selection[module])) for module in ordered]
    try:
        return await asyncio.gather(*pending)
    finally:
        for task in pending:
            if not task.done():
                task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)


async def check(args):
    started = time.monotonic()
    selection = {} if args.static_only else select(ROOT, args.group, args.test)
    full = not args.group and not args.test and not args.static_only
    print(f"{'FULL' if full else 'SELECTED'} check · {args.jobs} test processes", flush=True)
    for name, command in (
        ("format", [sys.executable, "-m", "ruff", "format", "--check", "src", "tests", "scripts"]),
        ("lint", [sys.executable, "-m", "ruff", "check", "src", "tests", "scripts"]),
        ("types", [sys.executable, "-m", "mypy"]),
    ):
        result = await execute(name, command, ROOT, args.timeout)
        display(result, args.verbose)
        if result.code:
            return 1
    needs_frontend = (
        args.frontend or full or bool(set(selection) & groups(ROOT)["web"] and not args.static_only)
    )
    if needs_frontend:
        if (ROOT / "frontend/node_modules").exists():
            result = await execute(
                "frontend build", ["npm", "--prefix", "frontend", "run", "build"], ROOT, args.timeout
            )
            display(result, args.verbose)
            if result.code:
                return 1
        else:
            print("SKIP frontend build · frontend/node_modules is absent", flush=True)
    if args.static_only:
        print(f"SELECTED checks passed · {time.monotonic() - started:.2f}s · tests not run", flush=True)
        return 0
    outcomes = await run_tests(ROOT, selection, args.jobs, args.timeout, args.verbose)
    passed = all(result.code == 0 for result in outcomes)
    print(
        f"{'FULL' if full else 'SELECTED'} checks {'passed' if passed else 'FAILED'} · "
        f"{sum(r.count for r in outcomes)} tests, {sum(r.skipped for r in outcomes)} skipped · "
        f"{time.monotonic() - started:.2f}s total",
        flush=True,
    )
    return 0 if passed else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--group", choices=GROUPS, action="append", default=[], help="Affected domain; repeat to combine"
    )
    parser.add_argument(
        "--test", action="append", default=[], help="Unittest module, class or method; repeat to combine"
    )
    parser.add_argument(
        "--jobs",
        type=int,
        default=min(3, os.cpu_count() or 1),
        help="Independent test processes (default: up to 3)",
    )
    parser.add_argument("--timeout", type=float, default=180, help="Seconds per command/test module")
    parser.add_argument(
        "--static-only", action="store_true", help="Format, lint and types only; not a full gate"
    )
    parser.add_argument("--frontend", action="store_true", help="Also build frontend for a selected check")
    parser.add_argument("--list", action="store_true", help="List groups without importing tests")
    parser.add_argument(
        "--verbose", action="store_true", help="Print successful command output and slow test durations"
    )
    args = parser.parse_args()
    if not 1 <= args.jobs <= 8 or not 0 < args.timeout <= 3600:
        parser.error("--jobs must be 1..8 and --timeout must be 0..3600 seconds")
    if args.static_only and (args.group or args.test):
        parser.error("--static-only cannot be combined with test selection")
    if args.list:
        for name, entries in groups(ROOT).items():
            print(f"{name}: {' '.join(sorted(entries))}")
        return 0
    try:
        return asyncio.run(check(args))
    except ValueError as exc:
        parser.error(str(exc))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
