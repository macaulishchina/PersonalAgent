"""Small maintenance interface; importing it requires no audio, model or MCP packages."""

from assistant_runtime.api.system import system
from assistant_runtime.platforms.ipc import connect

import argparse
import asyncio
import json
import logging
import os
from pathlib import Path
import sys
import base64
from assistant_runtime.app.config import Config


async def request(config, payload):
    reader, writer = await connect(config.socket, limit=4 * 1024 * 1024)
    try:
        writer.write((json.dumps(payload, ensure_ascii=False) + "\n").encode())
        await writer.drain()
        response = json.loads(
            await asyncio.wait_for(
                reader.readline(),
                max(
                    3690 if payload["command"] == "mcp" else 95,
                    config.model_api.get("timeout_seconds", 600) + 90,
                ),
            )
        )
        if not response["ok"]:
            raise RuntimeError(response["error"])
        return response["result"]
    finally:
        writer.close()
        await writer.wait_closed()


def systemctl(*args):
    from assistant_runtime.platforms.services import linux_systemctl

    return linux_systemctl(*args)


def main():
    from assistant_runtime.app.versions import CORE_VERSION

    parser = argparse.ArgumentParser(prog="pa", description="Personal Agent — run pa to chat")
    parser.add_argument("--version", action="version", version="PA " + CORE_VERSION)
    parser.add_argument(
        "--config", help="Use this config for this invocation; otherwise local or remembered instance"
    )
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("version", help="Show framework release without requiring an instance")
    upgrade = sub.add_parser(
        "upgrade-check", help="Inspect candidate wheels without installing or restarting"
    )
    upgrade.add_argument("wheel", help="Target personal-agent wheel")
    upgrade.add_argument(
        "--plugin-wheel", action="append", default=[], help="Optional replacement extension wheel; repeatable"
    )
    use = sub.add_parser("use", help="Remember an existing instance for future commands")
    use.add_argument("instance", help="Instance directory or entry TOML")
    chat = sub.add_parser(
        "chat", help="Chat with this instance; start its existing background manager if necessary"
    )
    chat.add_argument("--session", help="Select an existing stable conversation ID")
    install_pi = sub.add_parser("install-pi", help="Explicitly install the pinned Pi runtime")
    install_pi.add_argument("destination", nargs="?")
    service = sub.add_parser("service", help="Manage this instance's installed OS service")
    service.add_argument(
        "action", choices=["install", "start", "stop", "restart", "status", "logs", "uninstall"]
    )
    service.add_argument("--account", help="Explicit Windows service account")
    init = sub.add_parser("init", help="Create a private instance from a plugin preset")
    init.add_argument("destination")
    init.add_argument(
        "--preset", choices=["slim", "standard"], help="Default: slim on Windows, standard on Linux"
    )
    prepare = sub.add_parser(
        "prepare-config", help="Prepare an explicit plugin configuration from an older assistant instance"
    )
    prepare.add_argument("destination")
    executions = sub.add_parser("executions", help="Submit and inspect individual agent executions")
    executions.add_argument(
        "action",
        choices=[
            "submit",
            "get",
            "events",
            "cancel",
            "answer",
            "supplement",
            "message_get",
            "reconcile",
            "observations",
        ],
    )
    executions.add_argument(
        "arguments", help="JSON operation arguments; submit requires a stable request_id UUID"
    )
    files = sub.add_parser("files", help="Register and inspect bounded input resources")
    files.add_argument(
        "action",
        choices=[
            "put",
            "get",
            "finalize",
            "artifact_get",
            "artifact_read",
            "artifact_retain",
            "artifact_release",
        ],
    )
    files.add_argument("arguments", help="JSON: put accepts name, base64 data, and optional kind=file|image")
    management = sub.add_parser("configuration", help="Independent operator configuration console")
    management.add_argument("action", choices=["serve", "token", "reset-token", "check", "apply", "status"])
    management.add_argument("--management", required=True, help="Independent management.toml path")
    run = sub.add_parser("run")
    run.add_argument("--no-mic", action="store_true")
    for name in [
        "status",
        "doctor",
        "plugins",
        "wake",
        "sleep",
        "stop",
        "cancel",
        "progress",
        "history",
        "start",
        "restart",
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
            "reconcile_run",
            "resolve_run",
            "history",
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
    models.add_argument("action", choices=["check", "list", "status", "health", "infer"])
    models.add_argument("arguments", nargs="?", default="{}")
    accounts = sub.add_parser(
        "model-accounts", help="Pi-native model provider accounts; explicit operator login"
    )
    accounts.add_argument("action", choices=["providers", "status", "catalog", "login", "logout"])
    accounts.add_argument("--account")
    args = parser.parse_args()
    args.command = args.command or "chat"
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        from .instance_selection import resolve, select

        if args.command == "version":
            from assistant_runtime.app.versions import framework

            print(json.dumps(framework(), indent=2))
            return

        if args.command == "use":
            if args.config:
                raise ValueError("Use pa use <instance>, without --config")
            print(f"Selected {select(args.instance)}\nRun pa to chat.")
            return
        if args.command not in {"init", "install-pi", "configuration"}:
            entry = resolve(
                args.config, required=not (args.command == "model-accounts" and args.action == "providers")
            )
            args.config = str(entry) if entry else None
        if args.command == "plugins":
            from .release_inventory import inventory

            result = inventory(args.config)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            if not result["compatible"]:
                raise SystemExit(2)
            return
        if args.command == "upgrade-check":
            from .release_package import preflight

            result = preflight(args.config, args.wheel, args.plugin_wheel)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            if not result["compatible"]:
                raise SystemExit(2)
            return
        if args.command == "model-accounts":
            from assistant_runtime.app.config import read_config, default_root
            from assistant_runtime.core.models.settings import ModelSettings
            from assistant_runtime.core.pi.accounts import manage

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
                    asyncio.run(
                        manage(root, settings, args.action, args.account, binary=data.get("pi_binary", "pi"))
                    ),
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return
        if args.command == "init":
            from .initialize import initialize

            print(initialize(args.destination, preset=args.preset))
            return
        if args.command == "prepare-config":
            from .prepare_config import prepare

            if not args.config:
                raise ValueError("prepare-config requires an explicit --config source")
            print(json.dumps(prepare(args.config, args.destination), ensure_ascii=False, indent=2))
            return
        if args.command == "configuration":
            from assistant_runtime.core.configuration.settings import ManagementSettings
            from assistant_runtime.core.configuration.auth import AdminAccess

            settings = ManagementSettings.load(args.management)
            if args.action == "check":
                from assistant_runtime.core.configuration.service import ConfigurationService

                print(json.dumps(ConfigurationService(settings).check(), ensure_ascii=False))
            elif args.action in {"apply", "status"}:
                from assistant_runtime.core.configuration.client import operate

                print(json.dumps(asyncio.run(operate(settings, args.action)), ensure_ascii=False))
            elif args.action == "serve":
                from assistant_runtime.app.management import serve

                asyncio.run(serve(settings))
            else:
                path = settings.root / "state/configuration/admin-token"
                if args.action == "reset-token":
                    from assistant_runtime.core.configuration.files import atomic
                    from assistant_runtime.api.configuration_io import configuration_lock
                    import secrets

                    with configuration_lock(settings.root):
                        atomic(path, (secrets.token_urlsafe(32) + "\n").encode())
                AdminAccess(path)
                print(path.read_text(encoding="utf-8").strip())
            return
        if args.command == "run" and os.environ.get("PERSONAL_AGENT_SUPERVISOR_PID") != str(os.getppid()):
            from .management import serve
            from assistant_runtime.core.configuration.settings import ManagementSettings
            from .management_bootstrap import resolve_management

            asyncio.run(serve(ManagementSettings.load(resolve_management(args.config))))
            return
        if args.command == "install-pi":
            from .install_pi import setup

            print(setup(args.destination))
            return
        if args.command == "service" and args.action == "install":
            from .install_service import main as install_service

            if not args.config:
                parser.error("service install requires --config")
            argv = ["--config", str(args.config)]
            if args.account:
                argv.extend(["--account", args.account])
            install_service(argv)
            return
        if args.command == "service" or args.command in {"start", "restart", "stop", "logs"}:
            from .management_bootstrap import control_config
            from assistant_runtime.platforms.services import control

            result = control(
                args.action if args.command == "service" else args.command, control_config(args.config)
            )
            if result is not None:
                print(result)
            return
        if args.command == "status":
            from .management_bootstrap import control_config

            config = control_config(args.config)
        else:
            config = Config.load(args.config)
        if args.command == "chat":
            from .chat import chat as enter_chat

            asyncio.run(enter_chat(config, args.config, getattr(args, "session", None)))
        elif args.command == "run":
            from assistant_runtime.app.host import AssistantService

            asyncio.run(AssistantService(config, args.no_mic).run())
        elif args.command == "ai-usage" and args.action == "check":
            from assistant_runtime.core.ai_usage.settings import UsageSettings

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
        elif args.command == "models" and args.action == "check":
            from assistant_runtime.core.models.settings import ModelSettings

            settings = ModelSettings.parse(config.model_api)
            result = {
                "valid": True,
                "enabled": settings.enabled,
                "models": settings.catalog(),
                "revision": settings.fingerprint(),
            }
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
                from assistant_runtime.plugins.media.private import save_bytes

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
                    system.private(path)
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
                system.private(path)
                result["saved_to"] = str(path)
            print(json.dumps(result, ensure_ascii=False, indent=2))
    except (OSError, RuntimeError, ValueError, TimeoutError, KeyError) as exc:
        print(f"pa: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
