"""Small maintenance interface; importing it requires no audio, model or MCP packages."""

import argparse
import asyncio
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
from .config import Config


async def request(config, payload):
    reader, writer = await asyncio.open_unix_connection(config.socket, limit=4 * 1024 * 1024)
    try:
        writer.write((json.dumps(payload, ensure_ascii=False) + "\n").encode())
        await writer.drain()
        response = json.loads(await asyncio.wait_for(reader.readline(), 95))
        if not response["ok"]:
            raise RuntimeError(response["error"])
        return response["result"]
    finally:
        writer.close()
        await writer.wait_closed()


def systemctl(*args):
    env = os.environ.copy()
    env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    env.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path={env['XDG_RUNTIME_DIR']}/bus")
    return subprocess.run(["systemctl", "--user", *args], env=env, check=True)


def main():
    parser = argparse.ArgumentParser(description="Personal Agent — local assistant runtime")
    parser.add_argument("--config", help="Instance TOML; otherwise PERSONAL_AGENT_CONFIG or XDG config")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("--no-mic", action="store_true")
    for name in [
        "status",
        "doctor",
        "wake",
        "sleep",
        "stop",
        "cancel",
        "progress",
        "history",
        "start",
        "restart",
        "halt",
        "shutdown",
        "logs",
    ]:
        sub.add_parser(name)
    for name in ["text", "say"]:
        sub.add_parser(name).add_argument("text")
    sub.add_parser("inject-wav").add_argument("path")
    for name in ["room", "gesture"]:
        sub.add_parser(name).add_argument("value")
    sub.add_parser("volume").add_argument("value", type=int, choices=range(101), metavar="0..100")
    devices = sub.add_parser("devices")
    devices.add_argument("action", choices=["list", "inspect"], nargs="?", default="list")
    devices.add_argument("device_id", nargs="?")
    memory = sub.add_parser("memory")
    memory.add_argument("action", choices=["search", "read", "remember", "forget", "export", "import"])
    memory.add_argument(
        "arguments", nargs="?", default="{}", help="JSON arguments; import accepts records array"
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        config = Config.load(args.config)
        if args.command == "run":
            if config.profile == "headless":
                from .headless import HeadlessHost

                asyncio.run(HeadlessHost(config).run())
            else:
                from .assistant.service import AssistantService

                asyncio.run(AssistantService(config, args.no_mic).run())
        elif args.command in {"start", "restart", "halt"}:
            systemctl("stop" if args.command == "halt" else args.command, config.unit_name)
        elif args.command == "logs":
            subprocess.run(
                ["journalctl", "--user", "-u", config.unit_name, "-n", "60", "--no-pager"], check=True
            )
        elif args.command == "doctor":
            import shutil

            print(
                json.dumps(
                    {
                        "profile": config.profile,
                        "state": str(config.state_dir),
                        "plugins": config.plugins,
                        "models_present": config.models.exists(),
                        "executables": {
                            n: shutil.which(n) for n in ["codex", "pactl", "pacat", "bluetoothctl"]
                        },
                        "config_source": args.config or os.environ.get("PERSONAL_AGENT_CONFIG", "XDG"),
                    },
                    indent=2,
                )
            )
        else:
            payload = {k: v for k, v in vars(args).items() if k != "config"}
            if "arguments" in payload:
                payload["arguments"] = json.loads(payload["arguments"])
            if "path" in payload:
                payload["path"] = str(Path(payload["path"]).resolve())
            print(json.dumps(asyncio.run(request(config, payload)), ensure_ascii=False, indent=2))
    except (OSError, RuntimeError, ValueError, TimeoutError, KeyError) as exc:
        print(f"personal-agent: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
