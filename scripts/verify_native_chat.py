"""Explicit native detached-manager acceptance; run outside an owned execution job."""

import argparse
import asyncio
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import uuid


async def verify():
    from assistant_runtime.app.config import Config
    from assistant_runtime.app.initialize import initialize
    from assistant_runtime.app.cli import request
    from assistant_runtime.app.chat import ensure_running
    from assistant_runtime.api.system import system
    from assistant_runtime.platforms.daemon import stop

    async def terminal(entry):
        # Feed terminal commands deterministically; the CLI, IPC and manager are real processes.
        driver = """
import sys
from assistant_runtime.api.system import system
from assistant_runtime.app.cli import main
commands = iter(['/new native-terminal', '/sessions', '/exit'])
async def read(*, secret):
    return next(commands)
system._provider.terminal_input = read
entry = sys.argv[1]
sys.argv = ['pa', 'use', entry]
main()
sys.argv = ['pa']
main()
"""
        env = dict(os.environ)
        profile = entry.parent.parent / "client-profile"
        profile.mkdir(exist_ok=True)
        env["LOCALAPPDATA"] = str(profile)
        env["XDG_CONFIG_HOME"] = str(profile)
        env.pop("PERSONAL_AGENT_CONFIG", None)
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            driver,
            str(entry),
            cwd=profile,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), 40)
            assert process.returncode == 0, stderr.decode(errors="replace")
            assert b"Disconnected" in stdout, stdout.decode(errors="replace")
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()

    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder) / "instance"
        entry = initialize(root, preset="slim")
        with entry.open("a", encoding="utf-8") as stream:
            stream.write('\nunit_name = "personal-agent-test-' + uuid.uuid4().hex + '.service"\n')
        management = root / "management.toml"
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        management.write_text(
            "root = "
            + json.dumps(str(root))
            + "\nentry = "
            + json.dumps(str(entry))
            + f'\nhost="127.0.0.1"\nport={port}\nstartup_seconds=20\n',
            encoding="utf-8",
        )
        config = Config.load(entry)
        private = root / "state/configuration"
        owner = private / "detached-manager.json"
        try:
            await asyncio.gather(*(terminal(entry) for _ in range(2)))
            original = owner.read_bytes()
            await ensure_running(config, entry, request)
            assert owner.read_bytes() == original
            result = await request(
                config,
                {"command": "conversation", "action": "open", "arguments": {"operator": "native-test"}},
            )
            assert "id" in result
            status = await request(config, {"command": "management-status"})
            assert status["startup_complete"]
            assert "cli" in status["plugins"]
            from assistant_runtime.platforms.services import control

            await asyncio.to_thread(control, "restart", config)
            await ensure_running(config, entry, request)
            assert owner.read_bytes() != original
            restored = await request(
                config,
                {"command": "conversation", "action": "open", "arguments": {"operator": "native-test"}},
            )
            assert restored["id"] == result["id"]
            print(
                "CLI: concurrent startup, same manager, reconnect, restart and stable conversation passed",
                flush=True,
            )
        finally:
            if owner.exists():
                stop(owner, management)
                async with asyncio.timeout(20):
                    while True:
                        try:
                            with (
                                (private / "manager.lock").open("a+b") as manager_lock,
                                (root / "state/service.lock").open("a+b") as worker_lock,
                            ):
                                system.lock(manager_lock, blocking=False)
                                system.lock(worker_lock, blocking=False)
                            break
                        except BlockingIOError:
                            await asyncio.sleep(0.05)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--installed", action="store_true")
    args = parser.parse_args()
    if not args.installed:
        source = str(Path(__file__).resolve().parents[1] / "src")
        sys.path.insert(0, source)
        os.environ["PYTHONPATH"] = source
    else:
        os.environ.pop("PYTHONPATH", None)
    asyncio.run(verify())
