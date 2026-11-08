"""Authenticated terminal ingress; execution remains a public PA capability."""

import asyncio
import hmac
import re
import time
from aiohttp import web
from assistant_runtime.api.conversation import ConversationRequest
from assistant_runtime.api.configuration import ConfigurationUpdate
from .declaration import DECLARATION
from .repository import Devices
from .settings import desired, DEVICE_FIELDS, NETWORK_FIELDS


class AiPassportPlugin:
    manifest = DECLARATION.manifest

    def __init__(self, context):
        self.context, self.settings = context, context.config
        self.connections = {}
        self.runner = None
        self.pair_attempts = []

    async def start(self, context):
        self.devices = context.repository(Devices)
        self.frontend = context.resource("http_frontend")
        app = web.Application(client_max_size=16384, middlewares=[self.guard])
        app.add_routes([web.post("/pair", self.pair), web.get("/stream", self.stream)])
        self.runner = web.AppRunner(app, access_log=None)
        context.own(self.close)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        self.frontend.publish("127.0.0.1", port)
        self.frontend.register_management(self.manage)
        context.spawn(self.changes(context.subscribe(capacity=64)), "passport-changes")

    async def changes(self, queue):
        while True:
            event = await queue.get()
            try:
                if event.kind == "conversation.changed":
                    for connection in tuple(self.connections.values()):
                        if connection.session == event.data.get("session_id"):
                            connection.changed.set()
            finally:
                queue.task_done()

    @web.middleware
    async def guard(self, request, handler):
        if not hmac.compare_digest(request.headers.get("X-PA-Frontend-Key", ""), self.frontend.key):
            raise web.HTTPUnauthorized()
        if request.headers.get("Origin") or request.headers.get("Cookie"):
            raise web.HTTPForbidden()
        try:
            return await handler(request)
        except PermissionError:
            raise web.HTTPUnauthorized() from None
        except (ValueError, KeyError, TypeError):
            raise web.HTTPBadRequest(text="invalid_device_request") from None

    async def manage(self, action, arguments):
        if action == "list":
            return {
                "devices": [
                    {
                        **d,
                        "connected": d["device_id"] in self.connections,
                        "configuration": self.devices.configuration_state(d["device_id"]),
                    }
                    for d in self.devices.list()
                ]
            }
        if action == "pair":
            now = time.monotonic()
            self.pair_attempts = [t for t in self.pair_attempts if now - t < 60]
            if len(self.pair_attempts) >= 10:
                raise ValueError("配对尝试过于频繁，请一分钟后重试")
            self.pair_attempts.append(now)
            name = arguments.get("name", "PP")
            if not isinstance(name, str) or not 1 <= len(name.strip()) <= 48:
                raise ValueError("设备名称须为 1—48 个字符")
            code = arguments.get("code")
            if not isinstance(code, str) or len(code) != 8 or not code.isascii() or not code.isdigit():
                raise ValueError("请输入设备屏幕上的八位配对数字")
            try:
                ident = self.devices.challenge(code)
            except PermissionError:
                raise ValueError("配对码无效或已过期，请核对设备屏幕") from None
            result = await self.context.invoke(
                ConversationRequest(
                    "create", {"title": name, "request_id": ident, "policy": {"capture_memory": True}}
                )
            )
            try:
                return self.devices.activate(code, name.strip(), result["id"])
            except PermissionError:
                raise ValueError("配对码已失效，请在设备刷新后重试") from None
        if action == "revoke":
            ident = arguments["device_id"]
            self.devices.revoke(ident)
            if ident in self.connections:
                await self.connections[ident].close()
            return {"revoked": True}
        raise ValueError("Unsupported terminal management action")

    def settings_for(self, device):
        return desired(self.settings, device)

    def configuration_for(self, device):
        values = self.settings_for(device)
        return self.devices.configuration(
            device, {k: values[k] for k in (*DEVICE_FIELDS, *NETWORK_FIELDS) if k in values}
        )

    async def prepare_configuration(self, settings):
        previous = self.settings

        async def commit():
            self.settings = settings

        async def rollback():
            self.settings = previous

        async def release(committed):
            if committed:
                # Values are sent only after the file transaction applies. Each
                # device acknowledges its own fields after safe, durable apply.
                results = await asyncio.gather(
                    *(c.send_configuration() for c in tuple(self.connections.values())),
                    return_exceptions=True,
                )
                for result in results:
                    if isinstance(result, Exception):
                        self.context.log.warning(
                            "passport_configuration_pending type=%s", type(result).__name__
                        )

        return ConfigurationUpdate(commit, rollback, release)

    async def pair(self, request):
        data = await request.json()
        if (
            set(data) != {"device_id", "credential", "protocol"}
            or type(data["protocol"]) is not int
            or data["protocol"] != 2
        ):
            raise ValueError("Invalid pairing")
        if any(
            not isinstance(data[k], str) or not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", data[k])
            for k in ("device_id", "credential")
        ):
            raise ValueError("Invalid pairing")
        # The device creates its own random credential; no administrator or
        # supplier credential is ever delivered to the terminal.
        return web.json_response({"protocol": 2, **self.devices.begin(data["device_id"], data["credential"])})

    async def stream(self, request):
        from .connection import Connection

        ident = request.headers.get("X-Device-Id", "")
        credential = request.headers.get("Authorization", "").removeprefix("Bearer ")
        session = self.devices.authenticate(ident, credential)
        previous = self.connections.get(ident)
        if previous is None and len(self.connections) >= self.settings["max_connections"]:
            raise web.HTTPServiceUnavailable(text="terminal_capacity")
        ws = web.WebSocketResponse(max_msg_size=16384, heartbeat=15)
        connection = Connection(self, ident, session, ws)
        self.connections[ident] = connection
        try:
            # Only this device's authenticated credential can supersede its
            # transport. Pairing/session/accepted work remain unchanged.
            if previous:
                try:
                    await previous.close()
                except Exception as exc:
                    self.context.log.warning(
                        "passport_previous_connection_cleanup_failed device=%s type=%s",
                        ident,
                        type(exc).__name__,
                    )
            if self.connections.get(ident) is not connection or connection.closed:
                raise web.HTTPConflict(text="device_connection_superseded")
            await ws.prepare(request)
            await connection.run()
        finally:
            try:
                await connection.close()
            finally:
                # Old handler completion must never remove its replacement.
                if self.connections.get(ident) is connection:
                    self.connections.pop(ident, None)
        return ws

    async def close(self):
        self.frontend.withdraw()
        await asyncio.gather(*(c.close() for c in tuple(self.connections.values())), return_exceptions=True)
        if self.runner:
            await self.runner.cleanup()
