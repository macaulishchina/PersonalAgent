"""Native terminal proxy: explicit registered routes, no browser or administrator cookies."""

import asyncio
import ipaddress
import re
from aiohttp import (
    ClientSession,
    ClientTimeout,
    DummyCookieJar,
    WSMsgType,
    WSServerHandshakeError,
    ClientError,
    web,
)


class EntranceGateway:
    def __init__(self, business):
        self.business = business
        self.client = None
        self.sockets = set()
        self.pending = 0

    async def close(self):
        await asyncio.gather(*(s.close() for s in tuple(self.sockets)), return_exceptions=True)
        if self.client:
            await self.client.close()

    async def forward(self, request):
        owner, route = request.match_info["owner"], request.match_info["route"]
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", owner) or (request.method, route) not in {
            ("GET", "stream"),
            ("POST", "pair"),
        }:
            raise web.HTTPNotFound()
        if request.headers.get("Origin") or request.headers.get("Cookie"):
            raise web.HTTPForbidden()
        if self.pending + len(self.sockets) >= 16:
            raise web.HTTPServiceUnavailable(text="entrance_capacity")
        self.pending += 1
        downstream = None
        try:
            endpoint = await self.business.request({"command": "http-frontend", "owner": owner})
            if not endpoint:
                raise web.HTTPServiceUnavailable(text="entrance_unavailable")
            host, port = endpoint["host"], endpoint["port"]
            if not ipaddress.ip_address(host).is_loopback or type(port) is not int or not 1 <= port <= 65535:
                raise web.HTTPServiceUnavailable(text="entrance_unavailable")
            if self.client is None:
                self.client = ClientSession(
                    cookie_jar=DummyCookieJar(), trust_env=False, timeout=ClientTimeout(total=None, connect=5)
                )
            headers = {"X-PA-Frontend-Key": endpoint["key"]}
            for key in ("Authorization", "X-Device-Id"):
                if key in request.headers:
                    headers[key] = request.headers[key]
            address = f"http://{host}:{port}/{route}"
            if route == "pair":
                if request.content_length is None or request.content_length > 4096:
                    raise web.HTTPRequestEntityTooLarge(
                        max_size=4096, actual_size=request.content_length or 4097
                    )
                body = await asyncio.wait_for(request.read(), 5)
                async with self.client.post(
                    address,
                    headers={**headers, "Content-Type": "application/json"},
                    data=body,
                    allow_redirects=False,
                    timeout=ClientTimeout(total=10),
                ) as response:
                    result = await response.content.read(4097)
                    if len(result) > 4096:
                        raise web.HTTPBadGateway()
                    return web.Response(
                        status=response.status,
                        body=result,
                        content_type="application/json",
                        headers={"Cache-Control": "no-store"},
                    )
            async with self.client.ws_connect(
                address, headers=headers, max_msg_size=16384, heartbeat=15
            ) as upstream:
                downstream = web.WebSocketResponse(max_msg_size=16384, heartbeat=15)
                await downstream.prepare(request)
                self.sockets.add(downstream)

                async def pump(source, target):
                    async for message in source:
                        if message.type == WSMsgType.TEXT:
                            await asyncio.wait_for(target.send_str(message.data), 5)
                        elif message.type == WSMsgType.BINARY:
                            await asyncio.wait_for(target.send_bytes(message.data), 5)
                        else:
                            return

                workers = [
                    asyncio.create_task(pump(downstream, upstream)),
                    asyncio.create_task(pump(upstream, downstream)),
                ]
                try:
                    done, _ = await asyncio.wait(workers, return_when=asyncio.FIRST_COMPLETED)
                    for task in done:
                        task.result()
                finally:
                    for task in workers:
                        task.cancel()
                    await asyncio.gather(*workers, return_exceptions=True)
                return downstream
        except WSServerHandshakeError as exc:
            return web.Response(
                status=exc.status if exc.status in {400, 401, 403, 409, 503} else 502,
                text="terminal_connection_rejected",
            )
        except (ClientError, OSError, ValueError, TimeoutError):
            if downstream is None:
                raise web.HTTPServiceUnavailable(text="entrance_unavailable") from None
            return downstream
        finally:
            self.pending -= 1
            if downstream:
                self.sockets.discard(downstream)
                await downstream.close()
