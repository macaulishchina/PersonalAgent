"""PanSou adapter: bounded public search; backend labels are not trusted domains."""

import asyncio
from urllib.parse import urlsplit, parse_qs
import re
import time

from .errors import MediaError

ALIYUN_HOSTS = {"www.alipan.com", "alipan.com", "www.aliyundrive.com", "aliyundrive.com"}


def share_id(url):
    if not isinstance(url, str) or len(url) > 1000:
        raise ValueError("Invalid Aliyun share URL")
    parsed = urlsplit(url)
    match = re.fullmatch(r"/s/([A-Za-z0-9]{5,64})/?", parsed.path)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in ALIYUN_HOSTS
        or parsed.port not in (None, 443)
        or parsed.username
        or parsed.password
        or parsed.fragment
        or not match
    ):
        raise ValueError("Expected an official Aliyun HTTPS share URL")
    return match[1]


def extraction_code(url, explicit=""):
    share_id(url)
    if not isinstance(explicit, str):
        raise ValueError("Invalid share extraction code")
    values = parse_qs(urlsplit(url).query).get("pwd", [])
    password = explicit or (values[0] if len(values) == 1 else "")
    if not isinstance(password, str) or len(password) > 32 or len(values) > 1:
        raise ValueError("Invalid share extraction code")
    return password


async def json_response(response, *, limit=2 * 1024 * 1024):
    import json

    data = bytearray()
    async for chunk in response.content.iter_chunked(65536):
        data.extend(chunk)
        if len(data) > limit:
            raise MediaError("service_response_limit")
    try:
        value = json.loads(data)
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except ValueError:
        raise MediaError("service_invalid_response") from None


class PanSou:
    def __init__(self, session, url):
        self.session, self.url = session, url
        self.lock = asyncio.Lock()
        self.cache = {}

    async def search(self, query, before=0, limit=20):
        import aiohttp

        if not isinstance(query, str) or not 1 <= len(query.strip()) <= 150:
            raise ValueError("Search query must be 1..150 characters")
        if (
            type(limit) is not int
            or not 1 <= limit <= 50
            or type(before) is not int
            or not 0 <= before <= 500
        ):
            raise ValueError("Invalid search page")
        async with self.lock:
            cached = self.cache.get(query)
            if not cached or time.monotonic() - cached[0] > 30:
                try:
                    async with self.session.post(
                        self.url + "/api/search",
                        json={
                            "kw": query,
                            "src": "all",
                            "res": "all",
                            "cloud_types": ["aliyun"],
                        },
                        allow_redirects=False,
                    ) as response:
                        if response.status != 200:
                            raise MediaError("search_unavailable")
                        body = await json_response(response)
                except (aiohttp.ClientError, OSError, TimeoutError):
                    raise MediaError("search_unavailable") from None
                if body.get("code") != 0 or not isinstance(body.get("data"), dict):
                    raise MediaError("search_invalid_response")
                results = body["data"].get("results") or []
                if not isinstance(results, list):
                    raise MediaError("search_invalid_response")
                found = {}
                for row in results[:500]:
                    if not isinstance(row, dict):
                        continue
                    links = row.get("links") or []
                    if not isinstance(links, list):
                        continue
                    for link in links[:30]:
                        if not isinstance(link, dict):
                            continue
                        try:
                            ident = share_id(link.get("url"))
                            password = extraction_code(link["url"], link.get("password") or "")
                        except ValueError:
                            continue
                        found.setdefault(
                            ident,
                            {
                                "share_url": "https://www.alipan.com/s/" + ident,
                                "title": str(row.get("title") or "")[:300],
                                "password": password,
                                "source": str(row.get("channel") or row.get("unique_id") or "")[:160],
                                "verified": False,
                            },
                        )
                cached = (time.monotonic(), list(found.values())[:500], time.time())
                if len(self.cache) >= 32:
                    self.cache.pop(next(iter(self.cache)))
                self.cache[query] = cached
            items = cached[1][before : before + limit]
            return {
                "items": items,
                "next": before + limit if before + limit < len(cached[1]) else None,
                "observed_at": cached[2],
                "complete": False,
                "note": "Public source coverage is partial; candidates may be stale or mismatched. Inspect actual files before saving.",
            }
