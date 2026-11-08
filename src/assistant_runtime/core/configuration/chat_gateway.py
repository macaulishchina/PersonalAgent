"""Stream the optional chat entrance through the independent management listener."""

import asyncio
import ipaddress
import re
from urllib.parse import urlsplit

from aiohttp import ClientSession, ClientTimeout, DummyCookieJar, ClientError, web
from multidict import CIMultiDict
from yarl import URL

HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}


def headers(source):
    excluded = HOP | {p.strip().lower() for p in source.get("Connection", "").split(",")}
    return CIMultiDict(
        (k, v) for k, v in source.items() if k.lower() not in excluded and not k.lower().startswith("x-pa-")
    )


class ChatGateway:
    def __init__(self, business):
        self.business = business
        self.client = None

    async def close(self):
        if self.client:
            await self.client.close()

    async def forward(self, request):
        if request.path == "/chat":
            raise web.HTTPPermanentRedirect("/chat/")
        process = self.business.process
        if process is None or process.returncode is not None:
            raise web.HTTPServiceUnavailable(text="聊天服务未运行；可通过 /config/ 访问配置页面。")
        try:
            endpoint = await self.business.request({"command": "http-frontend", "owner": "web"})
        except (OSError, ValueError, TimeoutError):
            raise web.HTTPServiceUnavailable(text="聊天服务正在启动或重启；请稍后重试。") from None
        if not endpoint:
            raise web.HTTPNotFound(text="聊天网页未启用；可在 /config/ 的功能与插件中开启。")
        host, port = endpoint["host"], endpoint["port"]
        if not ipaddress.ip_address(host).is_loopback or type(port) is not int or not 1 <= port <= 65535:
            raise web.HTTPServiceUnavailable(text="聊天入口不可用。")
        if self.client is None:
            self.client = ClientSession(cookie_jar=DummyCookieJar(), auto_decompress=False, trust_env=False)
        outgoing = headers(request.headers)
        # Never disclose the administrator credential/session to the chat plugin.
        outgoing.popall("Cookie", None)
        cookies = []
        for name in (
            "personal_agent_session",
            "personal_agent_view",
            "personal_agent_access_request",
            "personal_agent_visit",
        ):
            cookie = request.cookies.get(name)
            if cookie and re.fullmatch(r"[A-Za-z0-9_-]{43}", cookie):
                cookies.append(name + "=" + cookie)
        if cookies:
            outgoing["Cookie"] = "; ".join(cookies)
        outgoing["X-PA-Frontend-Key"] = endpoint["key"]
        outgoing["X-PA-Frontend-Scheme"] = request["gateway_scheme"]
        outgoing["X-PA-Frontend-Origin"] = request.get(
            "gateway_origin", request["gateway_scheme"] + "://" + request.host
        )
        outgoing["X-PA-Frontend-Trusted-Origin"] = "1" if request.get("gateway_trusted_origin") else "0"
        outgoing["Host"] = urlsplit(outgoing["X-PA-Frontend-Origin"]).netloc
        outgoing["X-PA-Frontend-Peer"] = request.remote or "unknown"
        target = URL.build(scheme="http", host=host, port=port).with_path("/")
        path = request.raw_path if request.path == "/" else request.raw_path[len("/chat") :]
        target = URL(str(target).rstrip("/") + path, encoded=True)

        async def body():
            size = 0
            async for chunk in request.content.iter_chunked(65536):
                size += len(chunk)
                if size > 11 * 1024 * 1024:
                    raise web.HTTPRequestEntityTooLarge(max_size=11 * 1024 * 1024, actual_size=size)
                yield chunk

        streaming = bool(
            re.fullmatch(
                r"/chat/(?:c/[a-f0-9]{32}/|e/[a-f0-9]{32}/(?:account|visit)/)?api/events", request.path
            )
        )
        timeout = ClientTimeout(total=None if streaming else 120, connect=5)
        response = None
        try:
            async with self.client.request(
                request.method,
                target,
                headers=outgoing,
                data=body() if request.can_read_body else None,
                allow_redirects=False,
                timeout=timeout,
            ) as upstream:
                incoming = headers(upstream.headers)
                if incoming.get("Location", "").startswith("/") and not incoming["Location"].startswith("//"):
                    incoming["Location"] = "/chat" + incoming["Location"]
                response = web.StreamResponse(status=upstream.status, headers=incoming)
                await response.prepare(request)
                async for chunk in upstream.content.iter_any():
                    await response.write(chunk)
                await response.write_eof()
                return response
        except asyncio.CancelledError:
            if response is not None:
                response.force_close()
            raise
        except (ClientError, OSError, TimeoutError):
            if response is not None and response.prepared:
                response.force_close()
                return response
            raise web.HTTPServiceUnavailable(text="聊天服务连接已断开；请稍后重试。") from None
