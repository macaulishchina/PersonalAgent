"""Attach the explicit CLI entrance to one existing instance, starting its owner if needed."""

import asyncio
import time
from pathlib import Path
from assistant_runtime.api.system import system
from assistant_runtime.core.configuration.settings import ManagementSettings
from .management_bootstrap import resolve_management


async def ensure_running(config, entry, request):
    if not {"conversation", "cli"}.issubset(config.enabled_plugins):
        raise ValueError(
            "Enable conversation and cli in this instance before chatting; use prepare-config for a candidate"
        )
    path = Path(resolve_management(entry))
    settings = ManagementSettings.load(path)
    if settings.root != config.root.resolve() or (entry and settings.entry != Path(entry).resolve()):
        raise ValueError("Management bootstrap does not belong to the selected instance")
    private = settings.root / "state/configuration"
    private.mkdir(parents=True, exist_ok=True)
    system.private(private, directory=True)
    deadline = time.monotonic() + settings.startup_seconds + 10

    async def ready():
        try:
            async with asyncio.timeout(2):
                state = await request(config, {"command": "management-status"})
        except PermissionError:
            raise
        except (OSError, TimeoutError):
            return False
        if state.get("startup_complete"):
            if not {"conversation", "cli"}.issubset(state.get("plugins", {})):
                raise ValueError(
                    "Running configuration has no CLI conversation entrance; apply the updated configuration first"
                )
            failed = [
                name for name in state.get("critical", []) if state["plugins"][name]["state"] != "ready"
            ]
            if failed:
                raise RuntimeError("Instance startup failed: " + ", ".join(failed))
            return True
        return False

    if await ready():
        return
    lock_path = private / "chat-start.lock"
    with lock_path.open("a+b") as lock:
        system.private(lock_path)
        while True:
            try:
                system.lock(lock, blocking=False)
                break
            except BlockingIOError:
                if await ready():
                    return
                if time.monotonic() >= deadline:
                    raise TimeoutError("Timed out waiting for another CLI startup")
                await asyncio.sleep(0.1)
        if await ready():
            return
        # A running manager with a failed business process must be repaired, not duplicated.
        with (private / "manager.lock").open("a+b") as owned:
            try:
                system.lock(owned, blocking=False)
            except BlockingIOError:
                manager_running = True
            else:
                manager_running = False
        process = None
        owner_path = private / "detached-manager.json"
        if not manager_running and owner_path.exists():
            import json
            from assistant_runtime.platforms.daemon import running

            owner = json.loads(owner_path.read_text())
            if Path(owner["management"]).resolve() != path.resolve():
                raise ValueError("Detached manager record belongs to another instance")
            manager_running = running(owner)
        if not manager_running:
            from assistant_runtime.platforms.daemon import installed, launch

            if installed(config, path):
                from assistant_runtime.platforms.services import control

                await asyncio.to_thread(control, "start", config)
            else:
                process = launch(path, private / "manager.log", private / "detached-manager.json")
        while time.monotonic() < deadline:
            if await ready():
                return
            if process and process.poll() is not None:
                raise RuntimeError(
                    f"Management startup exited ({process.returncode}); inspect {private / 'manager.log'}"
                )
            await asyncio.sleep(0.15)
    raise TimeoutError(
        f"Instance was not ready; inspect {private / 'business.log'} and {private / 'manager.log'}. Existing processes were not replaced."
    )


async def chat(config, entry, session=None):
    from .cli import request
    from assistant_runtime.core.cli.client import Chat

    try:
        await ensure_running(config, entry, request)
    except (OSError, RuntimeError, ValueError) as exc:
        raise RuntimeError(
            f"{exc}; instance logs: {config.state_dir / 'configuration' / 'manager.log'} "
            f"and {config.state_dir / 'configuration' / 'business.log'}"
        ) from exc

    async def invoke(action, arguments):
        return await request(config, {"command": "conversation", "action": action, "arguments": arguments})

    await Chat(invoke, attachment_root=config.state_dir / "web-uploads").run(session)
