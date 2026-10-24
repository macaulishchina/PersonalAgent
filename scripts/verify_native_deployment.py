"""Isolated native preset/restart acceptance. No credentials, providers or devices.

Run with the application interpreter. --installed excludes repository source.
This does not install a service or change an existing instance.
"""

import argparse
import asyncio
import json
import os
from pathlib import Path
import socket
import sys
import tempfile


def free_port():
    with socket.socket() as stream:
        stream.bind(("127.0.0.1", 0))
        return stream.getsockname()[1]


async def verify(preset, root):
    import tomlkit
    from aiohttp import ClientSession
    from assistant_runtime.api.system import system
    from assistant_runtime.app.initialize import initialize
    from assistant_runtime.app.config import Config
    from assistant_runtime.platforms.ipc import connect

    entry = initialize(root, preset=preset)
    config = Config.load(entry)
    if preset == "slim":
        assert not set(config.plugins) & {"web", "conversation", "tasks", "devices", "memory"}
    assert not config.model_api.get("enabled", False)
    management = root / "management.toml"
    data = tomlkit.parse(management.read_text(encoding="utf-8"))
    data.update(host="127.0.0.1", port=free_port())
    system.atomic_write(management, tomlkit.dumps(data).encode())
    services = root / "config/services.toml"
    if services.exists():
        content = services.read_text(encoding="utf-8").replace("8046", str(free_port()))
        system.atomic_write(services, content.encode())
    env = {key: value for key, value in os.environ.items() if not key.startswith("PERSONAL_AGENT_")}
    env["PYTHONUTF8"] = "1"
    logpath = root / "acceptance.log"
    for restart in range(2):
        with logpath.open("ab") as log:
            process = await system.spawn(
                sys.executable,
                "-m",
                "assistant_runtime.app.cli",
                "--config",
                str(entry),
                "run",
                env=env,
                stdout=log,
                stderr=log,
            )
            try:
                async with asyncio.timeout(90), ClientSession() as client:
                    while True:
                        if process.returncode is not None:
                            raise RuntimeError(
                                "Management exited: " + logpath.read_text(encoding="utf-8")[-4000:]
                            )
                        try:
                            async with client.get(f"http://127.0.0.1:{data['port']}/api/status") as response:
                                assert response.status == 401, response.status
                            reader, writer = await connect(config.socket)
                            try:
                                writer.write(b'{"command":"management-status"}\n')
                                await writer.drain()
                                status = json.loads(await reader.readline())["result"]
                            finally:
                                writer.close()
                                await writer.wait_closed()
                            if status["startup_complete"]:
                                assert all(
                                    status["plugins"][name]["state"] == "ready" for name in status["critical"]
                                ), status
                                break
                        except (OSError, KeyError):
                            pass
                        await asyncio.sleep(0.1)
                print(f"{preset}: start {restart + 1}, console auth and business health passed", flush=True)
            finally:
                # Exercise abnormal owner loss, not only the application's stop hook.
                system.signal_process(process, force=True)
                await process.wait()
                await system.stop_process(process)
                async with asyncio.timeout(80):
                    while True:
                        try:
                            with (root / "state/service.lock").open("a+b") as lock:
                                system.lock(lock, blocking=False)
                            break
                        except BlockingIOError:
                            await asyncio.sleep(0.05)
    assert (root / "state/configuration/business-owner.json").exists()
    print(f"{preset}: completed; no provider call was configured", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--installed", action="store_true")
    parser.add_argument("--preset", choices=("slim", "standard", "both"), default="both")
    args = parser.parse_args()
    if not args.installed:
        source = str(Path(__file__).resolve().parents[1] / "src")
        sys.path.insert(0, source)
        os.environ["PYTHONPATH"] = source
    else:
        os.environ.pop("PYTHONPATH", None)

    async def run():
        with tempfile.TemporaryDirectory(prefix="agent 中文 ") as folder:
            for preset in ("slim", "standard") if args.preset == "both" else (args.preset,):
                await verify(preset, Path(folder) / preset)

    asyncio.run(run())


if __name__ == "__main__":
    main()
