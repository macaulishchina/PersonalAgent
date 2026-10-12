"""Hardware-free host for management and explicit trigger integration tests."""

import asyncio
import fcntl
import json
import os
import signal

from .kernel.contracts import Control, Trigger
from .kernel.runtime import Runtime, Manifest
from .plugins.gesture import GesturePlugin
from .store import Store


class HeadlessHost:
    manifest = Manifest("assistant", critical=True)

    def __init__(self, config):
        self.config = config
        self.triggers = 0
        self.last_source = None
        self.stop = asyncio.Event()
        config.state_dir.mkdir(parents=True, exist_ok=True)
        self.store = Store(config.state_dir / "assistant.sqlite3")
        self.runtime = Runtime(self.store)
        self.runtime.add(self)
        self.runtime.add(GesturePlugin())

    async def start(self, context):
        self.context = context

        async def trigger(request):
            self.triggers += 1
            self.last_source = request.source
            context.publish("assistant.triggered", source=request.source)
            return {"accepted": True, "voice_available": False}

        async def control(request):
            if request.kind not in {"sleep", "stop"}:
                raise ValueError("No device or brain configured in headless profile")
            return {"ok": True}

        context.register(Trigger, trigger)
        context.register(Control, control)

    async def connection(self, reader, writer):
        try:
            request = json.loads(await asyncio.wait_for(reader.readline(), 5))
            command = request["command"]
            if command == "gesture":
                await self.runtime.plugins["gesture"].simulate(request.get("value", "wave"))
            elif command == "wake":
                await self.runtime.invoke(Trigger("cli"))
            elif command == "shutdown":
                self.stop.set()
            elif command in {"sleep", "stop"}:
                await self.runtime.invoke(Control(command))
            elif command != "status":
                raise ValueError("Unsupported in headless profile")
            reply = {
                "ok": True,
                "result": {
                    "profile": "headless",
                    "plugins": self.runtime.health(),
                    "triggers": self.triggers,
                    "last_source": self.last_source,
                },
            }
        except Exception as exc:
            reply = {"ok": False, "error": str(exc)}
        writer.write((json.dumps(reply) + "\n").encode())
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    async def run(self):
        with (self.config.state_dir / "service.lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            await self.runtime.start()
            self.config.socket.unlink(missing_ok=True)
            server = await asyncio.start_unix_server(self.connection, path=self.config.socket)
            os.chmod(self.config.socket, 0o600)
            for sig in (signal.SIGTERM, signal.SIGINT):
                asyncio.get_running_loop().add_signal_handler(sig, self.stop.set)
            try:
                async with server:
                    await self.stop.wait()
            finally:
                await self.runtime.close()
                self.config.socket.unlink(missing_ok=True)
                self.store.db.close()
