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


async def verify(preset, root, extensions=(), windows_device_fixtures=False):
    import tomlkit
    from aiohttp import ClientSession
    from assistant_runtime.api.system import system
    from assistant_runtime.app.initialize import initialize
    from assistant_runtime.app.config import Config
    from assistant_runtime.platforms.ipc import connect

    from assistant_runtime.core.plugin_catalog import platforms

    unsupported = [name for name in ("web", "voice_disabled") if system.name not in platforms(name)]
    if preset == "standard" and unsupported:
        try:
            initialize(root, preset=preset)
        except ValueError as exc:
            assert all(word in str(exc) for word in (*unsupported, "slim")), str(exc)
        else:
            raise AssertionError("Windows standard initialization must reject uncertified extensions")
        assert not root.exists(), "Rejected initialization must not leave an instance directory"
        print("standard: rejected on Windows as required by extension certification", flush=True)
        return
    entry = initialize(root, preset=preset)
    if extensions:
        document = tomlkit.parse(entry.read_text(encoding="utf-8"))
        document["plugins"].extend(extensions)
        if windows_device_fixtures:
            if system.name != "windows" or not {"devices", "pipewire", "v4l2"} <= set(document["plugins"]):
                raise ValueError("Windows device fixtures require Windows and devices, pipewire, v4l2")
            document["devices"] = {
                "absent_microphone": {
                    "adapter": "pipewire",
                    "kind": "microphone",
                    "label": "Absent acceptance microphone",
                    "connection": {"endpoint_id": "{0.1.0.00000000}.{00000000-0000-0000-0000-000000000001}"},
                },
                "absent_camera": {
                    "adapter": "v4l2",
                    "kind": "camera",
                    "label": "Absent acceptance camera",
                    "connection": {
                        "device_path": r"@device_pnp_\\?\usb#vid_ffff&pid_ffff#pa-acceptance-absent"
                    },
                },
            }
        system.atomic_write(entry, tomlkit.dumps(document).encode())
    config = Config.load(entry)
    if preset == "slim":
        assert {"conversation", "cli"} <= set(config.plugins)
        assert not (set(config.plugins) - set(extensions)) & {
            "web",
            "assistant",
            "voice_disabled",
            "tasks",
            "devices",
            "memory",
        }
    assert not config.model_api.get("enabled", False)
    management = root / "management.toml"
    data = tomlkit.parse(management.read_text(encoding="utf-8"))
    data.update(host="127.0.0.1", port=free_port())
    system.atomic_write(management, tomlkit.dumps(data).encode())
    services = root / "config/services.toml"
    if "web" in config.plugins and not services.exists():
        document = tomlkit.parse(entry.read_text(encoding="utf-8"))
        document["web"] = {"host": "127.0.0.1", "port": free_port()}
        system.atomic_write(entry, tomlkit.dumps(document).encode())
    if services.exists():
        content = tomlkit.parse(services.read_text(encoding="utf-8"))
        if "web" in content:
            content["web"].update(host="127.0.0.1", port=0)
        system.atomic_write(services, tomlkit.dumps(content).encode())
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
                                assert all(
                                    status["plugins"][name]["state"] == "ready" for name in extensions
                                ), status
                                if windows_device_fixtures:
                                    installed = json.loads(
                                        (root / "state/plugin-installation.json").read_text(encoding="utf-8")
                                    )
                                    backends = {row["name"]: row["backend"] for row in installed["plugins"]}
                                    assert (
                                        backends["pipewire"] == "wasapi" and backends["v4l2"] == "directshow"
                                    ), backends
                                from assistant_runtime.app.versions import CORE_VERSION

                                receipt = json.loads(
                                    (root / "state/release-state.json").read_text(encoding="utf-8")
                                )
                                assert receipt["last_successful"]["core_version"] == CORE_VERSION
                                assert set(receipt["last_successful"]["plugins"]) == set(
                                    config.enabled_plugins
                                )
                                break
                        except (OSError, KeyError):
                            pass
                        await asyncio.sleep(0.1)
                print(f"{preset}: start {restart + 1}, console auth and business health passed", flush=True)
            except TimeoutError as exc:
                logs = [logpath, root / "state/configuration/business.log"]
                details = "\n".join(
                    f"{path.name}:\n{path.read_text(encoding='utf-8', errors='replace')[-6000:]}"
                    for path in logs
                    if path.is_file()
                )
                raise RuntimeError("Native startup timed out:\n" + details) from exc
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
    parser.add_argument(
        "--plugin", action="append", default=[], help="Additional plugin for the isolated instance"
    )
    parser.add_argument(
        "--windows-device-fixtures",
        action="store_true",
        help="Configure synthetic absent Windows audio/camera identities; no acquisition",
    )
    args = parser.parse_args()
    if not args.installed:
        source = str(Path(__file__).resolve().parents[1] / "src")
        sys.path.insert(0, source)
        os.environ["PYTHONPATH"] = os.pathsep.join(filter(None, (source, os.environ.get("PYTHONPATH"))))
    else:
        os.environ.pop("PYTHONPATH", None)

    async def run():
        with tempfile.TemporaryDirectory(prefix="agent 中文 ") as folder:
            for preset in ("slim", "standard") if args.preset == "both" else (args.preset,):
                await verify(preset, Path(folder) / preset, args.plugin, args.windows_device_fixtures)

    asyncio.run(run())


if __name__ == "__main__":
    main()
