import argparse
import asyncio
import json
import logging
import os
from pathlib import Path
import subprocess
import sys

from .config import Config

UNIT = "dell-voice-assistant.service"


async def request(config, payload):
    reader, writer = await asyncio.open_unix_connection(config.socket)
    try:
        writer.write((json.dumps(payload, ensure_ascii=False)+"\n").encode())
        await writer.drain()
        response = json.loads(await asyncio.wait_for(reader.readline(), 90))
        if not response["ok"]:
            raise RuntimeError(response["error"])
        return response["result"]
    finally:
        writer.close()
        await writer.wait_closed()


def systemctl(*args):
    env = os.environ.copy()
    env["XDG_RUNTIME_DIR"] = f"/run/user/{os.getuid()}"
    env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path=/run/user/{os.getuid()}/bus"
    return subprocess.run(["systemctl", "--user", *args], env=env, check=True)


def main():
    parser = argparse.ArgumentParser(description="嘟嘟 · DELL 常驻语音助手")
    parser.add_argument("--config")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("--no-mic", action="store_true", help="For integration tests without live microphone capture")
    for name in ["status", "doctor", "wake", "sleep", "stop", "cancel", "progress", "history", "start", "restart", "halt", "shutdown", "logs"]:
        sub.add_parser(name)
    for name in ["text", "say"]:
        p = sub.add_parser(name)
        p.add_argument("text")
    p = sub.add_parser("inject-wav")
    p.add_argument("path")
    p = sub.add_parser("room")
    p.add_argument("value", choices=["living", "study"])
    p = sub.add_parser("volume")
    p.add_argument("value", type=int, choices=range(101), metavar="0..100")
    args = parser.parse_args()
    config = Config.load(args.config)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        if args.command == "run":
            from .native_service import NativeService as Service
            asyncio.run(Service(config, args.no_mic).run())
        elif args.command in {"start", "restart"}:
            systemctl(args.command, UNIT)
        elif args.command == "halt":
            systemctl("stop", UNIT)
        elif args.command == "logs":
            subprocess.run(["journalctl", "--user", "-u", UNIT, "-n", "60", "--no-pager"], check=True)
        elif args.command == "doctor":
            from .audio import AudioRoute
            from .store import Store
            route = AudioRoute(config, Store(config.state_dir / "assistant.sqlite3"))
            sources, sinks = asyncio.run(route.devices())
            print(json.dumps({"sources": sources, "sinks": sinks, "models": [str(p.relative_to(config.root)) for p in (config.root/"models").rglob("*.onnx")], "codex": config.codex_binary}, ensure_ascii=False, indent=2))
        else:
            payload = {k: v for k, v in vars(args).items() if k != "config"}
            if "path" in payload:
                payload["path"] = str(Path(payload["path"]).resolve())
            print(json.dumps(asyncio.run(request(config, payload)), ensure_ascii=False, indent=2))
    except (OSError, RuntimeError, ValueError, TimeoutError) as exc:
        print(f"dell-voice: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
