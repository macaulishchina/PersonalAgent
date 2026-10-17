"""PanSou adapter: bounded public search; backend labels are not trusted domains."""

import asyncio
from urllib.parse import urlsplit, parse_qs
import re
import time
import unicodedata
import uuid
import weakref

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
        self.slots = asyncio.Semaphore(3)
        self.locks = weakref.WeakValueDictionary()
        self.snapshots = {}
        self.latest = {}
        self.pending = 0

    async def search(self, query, before=0, limit=20, *, search_id=None, refresh=False):
        """Refresh reads PanSou's asynchronously enriched cache, never starts a forced rescan.

        Every refresh creates a separate snapshot; existing pages remain immutable.
        Global coverage is always partial, even after all configured sources respond.
        """
        if not isinstance(query, str) or not 1 <= len(query.strip()) <= 150:
            raise ValueError("Search query must be 1..150 characters")
        query = query.strip()
        if (
            type(limit) is not int
            or not 1 <= limit <= 50
            or type(before) is not int
            or not 0 <= before <= 500
            or type(refresh) is not bool
            or search_id is not None
            and (not isinstance(search_id, str) or len(search_id) > 64)
        ):
            raise ValueError("Invalid search page")
        if before and not search_id or refresh and (search_id or before):
            raise ValueError("Continue pages with search_id; refresh starts a new first page")
        now = time.monotonic()
        for ident, snapshot in list(self.snapshots.items()):
            if now - snapshot["created"] > 300:
                del self.snapshots[ident]
        self.latest = {q: ident for q, ident in self.latest.items() if ident in self.snapshots}
        if search_id is not None:
            snapshot = self.snapshots.get(search_id)
            if not snapshot or snapshot["query"] != query:
                raise ValueError("Search snapshot expired or query changed; restart from the first page")
        else:
            if self.pending >= 8:
                raise MediaError("search_busy")
            self.pending += 1
            try:
                async with self.locks.setdefault(query, asyncio.Lock()):
                    snapshot = self.snapshots.get(self.latest.get(query))
                    if (
                        not snapshot
                        or time.monotonic() - snapshot["created"] > 30
                        or (refresh and snapshot["created"] < now)
                    ):
                        async with self.slots:
                            items = await self.fetch(query)
                            health = await self.health()
                        search_id = uuid.uuid4().hex
                        snapshot = {
                            "id": search_id,
                            "query": query,
                            "created": time.monotonic(),
                            "observed_at": time.time(),
                            "items": items,
                            "source_health": health,
                        }
                        if len(self.snapshots) >= 32:
                            del self.snapshots[next(iter(self.snapshots))]
                        self.snapshots[search_id] = snapshot
                        self.latest[query] = search_id
            finally:
                self.pending -= 1
        items = snapshot["items"][before : before + limit]
        return {
            "items": items,
            "search_id": snapshot["id"],
            "next": before + limit if before + limit < len(snapshot["items"]) else None,
            "observed_at": snapshot["observed_at"],
            "source_health": snapshot["source_health"],
            "complete": False,
            "refresh_after_seconds": 8,
            "note": "Partial public Aliyun coverage, not proof of absence. Slow sources may still be filling "
            "the backend cache. If insufficient, wait about 8 seconds and start a new first page with "
            "refresh=true; this rereads that cache, not a forced rescan. Browse this snapshot using search_id. "
            "Health summarizes recent searches, not this query's completion. Inspect actual files before saving.",
        }

    async def fetch(self, query):
        import aiohttp

        try:
            async with self.session.post(
                self.url + "/api/search",
                json={"kw": query, "src": "all", "res": "all", "cloud_types": ["aliyun"]},
                allow_redirects=False,
            ) as response:
                if response.status != 200:
                    raise MediaError("search_unavailable")
                body = await json_response(response)
        except (aiohttp.ClientError, OSError, TimeoutError):
            raise MediaError("search_unavailable") from None
        if body.get("code") != 0 or not isinstance(body.get("data"), dict):
            raise MediaError("search_invalid_response")
        results = body["data"].get("results")
        if results is None:
            results = []
        if not isinstance(results, list):
            raise MediaError("search_invalid_response")
        found = {}
        for row in results[:500]:
            if not isinstance(row, dict) or not isinstance(row.get("links"), list):
                continue
            for link in row["links"][:30]:
                if not isinstance(link, dict):
                    continue
                try:
                    ident = share_id(link.get("url"))
                    password = extraction_code(link["url"], link.get("password") or "")
                except ValueError:
                    continue
                title = str(link.get("work_title") or link.get("note") or row.get("title") or "")[:300]
                item = {
                    "share_url": "https://www.alipan.com/s/" + ident,
                    "title": title,
                    "context_title": str(row.get("title") or "")[:300],
                    "password": password,
                    "source": str(row.get("channel") or row.get("unique_id") or "")[:160],
                    "published_at": str(link.get("datetime") or row.get("datetime") or "")[:80],
                    "title_match": title_match(query, title),
                    "verified": False,
                }
                old = found.get(ident)
                if not old or item["title_match"] and not old["title_match"]:
                    found[ident] = item
        # Matching titles precede unrelated links in multi-title channel posts.
        # Keep unmatched candidates: filenames inside a share can provide better evidence.
        return sorted(found.values(), key=lambda item: not item["title_match"])[:500]

    async def health(self):
        import aiohttp

        try:
            async with self.session.get(
                self.url + "/api/health", timeout=aiohttp.ClientTimeout(total=2), allow_redirects=False
            ) as response:
                if response.status != 200:
                    return {"state": "unavailable"}
                body = await json_response(response, limit=65536)
            live = body.get("liveness") or {}
            return {
                "state": "available",
                "scope": "recent_searches",
                "failing_plugins": names(live.get("failing_plugins")),
                "zero_yield_plugins": names(live.get("zero_yield_plugins")),
                "zero_yield_channels": names(live.get("zero_yield_channels")),
            }
        except (aiohttp.ClientError, OSError, TimeoutError, MediaError, AttributeError):
            return {"state": "unavailable"}


def names(value):
    return (
        [str(item.get("name") or "")[:80] for item in value[:16] if isinstance(item, dict)]
        if isinstance(value, list)
        else []
    )


def title_match(query, title):
    def compact(value):
        return "".join(c for c in unicodedata.normalize("NFKC", value).casefold() if c.isalnum())

    query, title = compact(query), compact(title)
    base = re.sub(r"\d{4,8}", "", query)
    return query in title or bool(len(base) >= 2 and base in title)
