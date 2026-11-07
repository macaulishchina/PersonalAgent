"""Authenticated terminal ingress; execution remains a public PA capability."""

import asyncio
import hmac
import uuid
from urllib.parse import urlsplit
from aiohttp import web
from assistant_runtime.api.conversation import ConversationRequest
from .declaration import DECLARATION
from .repository import Devices


class AiPassportPlugin:
    manifest = DECLARATION.manifest

    def __init__(self, context):
        self.context, self.settings = context, context.config
        self.connections = {}
        self.runner = None

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
                    {**d, "connected": d["device_id"] in self.connections} for d in self.devices.list()
                ]
            }
        if action == "pair":
            name = arguments.get("name", "PP")
            if not isinstance(name, str) or not 1 <= len(name.strip()) <= 48:
                raise ValueError("设备名称须为 1—48 个字符")
            url = self.settings["public_url"]
            parsed = urlsplit(url)
            if (
                parsed.scheme != "wss"
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.query
                or parsed.fragment
                or not parsed.path.endswith("/stream")
            ):
                raise ValueError("请先配置公开 WSS 地址（以 /stream 结尾）")
            ident = uuid.uuid4().hex
            result = await self.context.invoke(
                ConversationRequest(
                    "create", {"title": name, "request_id": ident, "policy": {"capture_memory": True}}
                )
            )
            return {
                "protocol": 1,
                "url": url,
                **self.devices.issue(name, result["id"], ident),
                "settings": {
                    k: self.settings[k]
                    for k in ("nickname", "volume", "sleep_seconds", "input_mode", "keep_online")
                },
            }
        if action == "revoke":
            ident = arguments["device_id"]
            self.devices.revoke(ident)
            if ident in self.connections:
                await self.connections[ident].close()
            return {"revoked": True}
        raise ValueError("Unsupported terminal management action")

    async def pair(self, request):
        data = await request.json()
        if (
            set(data) != {"device_id", "pairing_token", "credential", "protocol"}
            or type(data["protocol"]) is not int
            or data["protocol"] != 1
        ):
            raise ValueError("Invalid pairing")
        if any(
            not isinstance(data[k], str) or not 16 <= len(data[k]) <= 128
            for k in ("device_id", "pairing_token", "credential")
        ):
            raise ValueError("Invalid pairing")
        self.devices.pair(data["device_id"], data["pairing_token"], data["credential"])
        return web.json_response({"paired": True, "protocol": 1})

    async def stream(self, request):
        from .connection import Connection

        ident = request.headers.get("X-Device-Id", "")
        credential = request.headers.get("Authorization", "").removeprefix("Bearer ")
        session = self.devices.authenticate(ident, credential)
        if ident in self.connections:
            raise web.HTTPConflict(text="device_already_connected")
        if len(self.connections) >= self.settings["max_connections"]:
            raise web.HTTPServiceUnavailable(text="terminal_capacity")
        ws = web.WebSocketResponse(max_msg_size=16384, heartbeat=15)
        connection = Connection(self, ident, session, ws)
        self.connections[ident] = connection
        try:
            await ws.prepare(request)
            await connection.run()
        finally:
            await connection.close()
            self.connections.pop(ident, None)
        return ws

    async def close(self):
        self.frontend.withdraw()
        await asyncio.gather(*(c.close() for c in tuple(self.connections.values())), return_exceptions=True)
        if self.runner:
            await self.runner.cleanup()
