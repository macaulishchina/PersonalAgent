"""Local operator client reuses console authentication and update coordination."""

import asyncio
import ipaddress
from aiohttp import ClientSession, ClientTimeout, CookieJar, ClientError


async def operate(settings, action):
    address = ipaddress.ip_address(settings.host)
    host = (
        "127.0.0.1"
        if address.is_unspecified and address.version == 4
        else "::1"
        if address.is_unspecified
        else settings.host
    )
    authority = f"[{host}]" if ":" in host else host
    origin = f"http://{authority}:{settings.port}"
    token_path = settings.root / "state/configuration/admin-token"
    try:
        async with ClientSession(
            cookie_jar=CookieJar(unsafe=True), timeout=ClientTimeout(total=15)
        ) as client:

            async def call(path, body=None):
                async with client.request(
                    "POST" if body is not None else "GET",
                    origin + path,
                    json=body,
                    headers={"Origin": origin},
                    allow_redirects=False,
                ) as response:
                    if response.status >= 400:
                        raise RuntimeError(
                            f"Configuration operation rejected ({response.status}); inspect the configuration page"
                        )
                    return await response.json()

            await call("/api/login", {"token": token_path.read_text(encoding="utf-8").strip()})
            if action == "status":
                return await call("/api/status")
            snapshot = await call("/api/configuration")
            return await call("/api/update", {"version": snapshot["version"]})
    except (ClientError, asyncio.TimeoutError, OSError) as exc:
        raise RuntimeError(
            f"Configuration manager unavailable; inspect {settings.root / 'state/configuration/manager.log'}"
        ) from exc
