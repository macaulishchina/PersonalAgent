"""Small maintenance interface; importing it requires no audio, model or MCP packages."""

import argparse
import asyncio
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import base64
from .config import Config


async def request(config, payload):
    reader, writer = await asyncio.open_unix_connection(config.socket, limit=4 * 1024 * 1024)
    try:
        writer.write((json.dumps(payload, ensure_ascii=False) + "\n").encode())
        await writer.drain()
        response = json.loads(
            await asyncio.wait_for(reader.readline(), 180 if payload["command"] == "mcp" else 95)
        )
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
    parser.add_argument(
        "--config", help="Entry TOML (supports include); otherwise PERSONAL_AGENT_CONFIG or XDG config"
    )
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
    sub.add_parser("gesture").add_argument("value")
    sub.add_parser("volume").add_argument("value", type=int, choices=range(101), metavar="0..100")
    devices = sub.add_parser("devices")
    devices.add_argument(
        "action",
        choices=[
            "list",
            "inspect",
            "snapshot",
            "record",
            "move",
            "stop-camera",
            "operations",
            "cancel-operation",
            "volume",
        ],
        nargs="?",
        default="list",
    )
    devices.add_argument("device_id", nargs="?")
    devices.add_argument("--operation-id", help="Operation identity; required for cancel-operation")
    devices.add_argument("--percent", type=int, choices=range(101), metavar="0..100")
    devices.add_argument(
        "--output", help="Save a requested snapshot (JPEG) or recorded clip (MP4) to this path"
    )
    devices.add_argument("--direction", choices=["left", "right", "up", "down"])
    devices.add_argument("--duration-ms", type=int, default=80, help="One camera step, 1..500 ms")
    devices.add_argument(
        "--duration", dest="duration_seconds", type=int, default=10, help="Video clip, 1..60 seconds"
    )
    events = sub.add_parser("events", help="Query records and manage trusted event sources")
    events.add_argument(
        "action",
        choices=[
            "capabilities",
            "sources",
            "configure",
            "enable",
            "remove",
            "verify",
            "status",
            "query",
            "get",
            "changes",
            "record",
            "correct",
            "delete",
            "stats",
        ],
    )
    events.add_argument("arguments", nargs="?", default="{}", help="JSON event arguments")
    notifications = sub.add_parser("notifications")
    notifications.add_argument(
        "action",
        choices=["list", "groups", "group", "get", "publish", "update", "read", "read_group", "retry"],
    )
    notifications.add_argument("arguments", nargs="?", default="{}", help="JSON notification arguments")
    search = sub.add_parser("search", help="Public web search and page reading through configured providers")
    search.add_argument("action", choices=["capabilities", "search", "fetch", "history"])
    search.add_argument("arguments", nargs="?", default="{}", help="JSON search arguments")
    media = sub.add_parser("media", help="Search, acquire and inspect media; plans remain in tasks")
    media.add_argument(
        "action",
        choices=[
            "status",
            "capabilities",
            "library",
            "preview",
            "apply",
            "collection",
            "asset",
            "batch",
            "batch_get",
            "batch_cancel",
            "search",
            "search_history",
            "search_sources",
            "inspect",
            "files",
            "transfer",
            "download",
            "get",
            "list",
            "cancel",
            "resume",
            "verify",
            "login",
            "login_status",
        ],
    )
    media.add_argument(
        "arguments", nargs="?", default="{}", help="JSON media arguments; never pass account credentials"
    )
    tasks = sub.add_parser("tasks")
    tasks.add_argument(
        "action",
        choices=[
            "list",
            "get",
            "models",
            "upgrade_model",
            "create",
            "update",
            "run",
            "pause",
            "resume",
            "terminate",
            "archive",
            "cancel_run",
            "pause_run",
            "continue",
            "supplement",
            "artifacts",
            "workfiles",
            "event_inbox",
            "trace",
        ],
    )
    tasks.add_argument("arguments", nargs="?", default="{}", help="JSON task arguments")
    memory = sub.add_parser("memory")
    memory.add_argument("action", choices=["search", "read", "remember", "forget", "export", "import"])
    memory.add_argument(
        "arguments", nargs="?", default="{}", help="JSON arguments; import accepts records array"
    )
    mcp = sub.add_parser("mcp", help="Manage this assistant instance's third-party MCPs")
    mcp.add_argument(
        "action",
        choices=["list", "get", "configure", "enable", "remove", "verify", "apply", "reload", "login"],
    )
    mcp.add_argument("arguments", nargs="?", default="{}", help="JSON MCP management arguments")
    decisions = sub.add_parser("decisions", help="Inspect semantic support and evaluate effect-free fixtures")
    decisions.add_argument(
        "action", choices=["status", "list", "get", "label", "evaluate", "reload", "archive"]
    )
    decisions.add_argument(
        "arguments", nargs="?", default="{}", help="JSON decision arguments; no credentials"
    )
    usage = sub.add_parser("ai-usage", help="AI service usage, budgets and explicit allowance extensions")
    usage.add_argument("action", choices=["check", "status", "summary", "calls", "stats", "extend"])
    usage.add_argument("arguments", nargs="?", default="{}")
    models = sub.add_parser("models", help="Model API configuration and bounded inference")
    models.add_argument("action", choices=["check", "gateway-config", "list", "status", "health", "infer"])
    models.add_argument("arguments", nargs="?", default="{}")
    accounts = sub.add_parser(
        "model-accounts", help="Pi-native model provider accounts; explicit operator login"
    )
    accounts.add_argument("action", choices=["providers", "status", "catalog", "login", "logout"])
    accounts.add_argument("--account")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        if args.command == "model-accounts":
            from .config import read_config, default_root
            from .plugins.models.settings import ModelSettings
            from .plugins.pi.accounts import manage

            # Authentication must remain operable while a disabled model binding is being repaired.
            source = (
                Path(
                    args.config
                    or os.environ.get("PERSONAL_AGENT_CONFIG")
                    or Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
                    / "personal-agent/config.toml"
                )
                .expanduser()
                .resolve()
            )
            data = read_config(source) if args.config or source.exists() else {}
            settings = ModelSettings.parse({"accounts": data.get("model_api", {}).get("accounts", {})})
            if args.action != "providers" and args.account not in settings.accounts:
                raise ValueError("Select a configured model account with --account")
            root = Path(data.get("root", source.parent if source.exists() else default_root())).expanduser()
            root = (root if root.is_absolute() else source.parent / root).resolve()
            print(
                json.dumps(
                    asyncio.run(manage(root, settings, args.action, args.account)),
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return
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
        elif args.command == "ai-usage" and args.action == "check":
            from .plugins.ai_usage.settings import UsageSettings

            policy = UsageSettings.parse(config.ai_usage)
            print(
                json.dumps(
                    {
                        "valid": True,
                        "configuration_revision": policy.revision(),
                        "max_requests": policy.max_requests,
                        "body_enabled": policy.body_enabled,
                    }
                )
            )
        elif args.command == "models" and args.action in {"check", "gateway-config"}:
            from .plugins.models.settings import ModelSettings
            from .plugins.models.gateway import configuration

            settings = ModelSettings.parse(config.model_api)
            result = (
                configuration(settings)
                if args.action == "gateway-config"
                else {
                    "valid": True,
                    "enabled": settings.enabled,
                    "models": settings.catalog(),
                    "revision": settings.fingerprint(),
                }
            )
            print(json.dumps(result, ensure_ascii=False, indent=2))
        elif args.command == "doctor":
            import shutil

            print(
                json.dumps(
                    {
                        "profile": config.profile,
                        "state": str(config.state_dir),
                        "plugins": config.plugins,
                        "models_present": config.models.exists(),
                        "executables": {n: shutil.which(n) for n in ["pi", "pactl", "pacat", "bluetoothctl"]},
                        "config_source": args.config or os.environ.get("PERSONAL_AGENT_CONFIG", "XDG"),
                    },
                    indent=2,
                )
            )
        else:
            payload = {k: v for k, v in vars(args).items() if k != "config"}
            output = payload.pop("output", None)
            if args.command == "devices" and (args.action != "list" and not args.device_id):
                raise ValueError("Device action requires a device ID")
            if args.command == "devices" and args.action == "cancel-operation" and not args.operation_id:
                raise ValueError("cancel-operation requires --operation-id")
            if args.command == "devices" and args.action == "volume" and args.percent is None:
                raise ValueError("devices volume requires --percent")
            if args.command == "devices" and args.action == "move":
                if not args.direction or not 1 <= args.duration_ms <= 500:
                    raise ValueError("move requires --direction and a duration of 1..500 ms")
            if output and (args.command != "devices" or args.action not in {"snapshot", "record"}):
                raise ValueError("--output is only valid for devices snapshot / record")
            if "arguments" in payload:
                payload["arguments"] = json.loads(payload["arguments"])
            if "path" in payload:
                payload["path"] = str(Path(payload["path"]).resolve())
            result = asyncio.run(request(config, payload))
            if args.command == "media" and "qr_base64" in result:
                from .plugins.media.private import save_bytes

                image = base64.b64decode(result.pop("qr_base64"), validate=True)
                path = config.state_dir / "media" / "login-qr.png"
                save_bytes(path, image)
                result["qr_path"] = str(path)
            if args.command == "devices" and args.action == "snapshot":
                image = base64.b64decode(result["frame"].pop("data_base64"), validate=True)
                result["frame"]["bytes"] = len(image)
                if output:
                    path = Path(output).expanduser().resolve()
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(image)
                    path.chmod(0o600)
                    result["saved_to"] = str(path)
                else:
                    result["saved_to"] = None
            elif args.command == "devices" and args.action == "record" and output:
                import shutil

                ident = result["video"]["id"]
                if len(ident) != 32 or any(c not in "0123456789abcdef" for c in ident):
                    raise ValueError("Invalid recorded video ID")
                path = Path(output).expanduser().resolve()
                path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(config.state_dir / "web-uploads" / ident, path)
                path.chmod(0o600)
                result["saved_to"] = str(path)
            print(json.dumps(result, ensure_ascii=False, indent=2))
    except (OSError, RuntimeError, ValueError, TimeoutError, KeyError) as exc:
        print(f"personal-agent: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
