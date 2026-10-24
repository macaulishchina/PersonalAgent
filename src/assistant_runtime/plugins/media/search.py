"""PanSou adapter: bounded public search; backend labels are not trusted domains."""

import asyncio
from urllib.parse import urlsplit, parse_qs
import re
import time
import unicodedata
import uuid
import weakref

from .errors import MediaError
from .search_candidates import acquisition, candidate_id, platform_filter, public_link

ALIYUN_HOSTS = {"www.alipan.com", "alipan.com", "www.aliyundrive.com", "aliyundrive.com"}


def share_id(url):
    if not isinstance(url, str) or len(url) > 1000:
        raise ValueError("Invalid media share URL")
    parsed = urlsplit(url)
    match = re.fullmatch(r"/s/([A-Za-z0-9]{5,64})/?", parsed.path)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in ALIYUN_HOSTS | {"pan.quark.cn"}
        or parsed.port not in (None, 443)
        or parsed.username
        or parsed.password
        or parsed.fragment
        or not match
    ):
        raise ValueError("Expected an official supported HTTPS share URL")
    return match[1]


def share_provider(url):
    share_id(url)
    return "quark" if urlsplit(url).hostname == "pan.quark.cn" else "aliyun"


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
    def __init__(self, session, url, history=None, accounts=None):
        self.session, self.url, self.history = session, url, history
        self.accounts = accounts or {"aliyun": {"provider": "aliyun"}}
        self.slots = asyncio.Semaphore(3)
        self.locks = weakref.WeakValueDictionary()
        self.snapshots = {}
        self.latest = {}
        self.pending = 0

    async def search(
        self, query, before=0, limit=20, *, search_id=None, refresh=False, aliases=None, platforms=None
    ):
        if not isinstance(query, str) or not 1 <= len(query.strip()) <= 150:
            raise ValueError("Search query must be 1..150 characters")
        query = query.strip()
        if aliases is None:
            aliases = []
        if (
            not isinstance(aliases, list)
            or len(aliases) > 3
            or any(not isinstance(q, str) or not 1 <= len(q.strip()) <= 150 for q in aliases)
        ):
            raise ValueError("At most three explicit title aliases, each 1..150 characters")
        queries = list(dict.fromkeys([query, *(q.strip() for q in aliases)]))
        platforms = platform_filter(platforms)
        key = (tuple(queries), tuple(platforms))
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
            if not snapshot or snapshot["key"] != key:
                raise ValueError("Search snapshot expired or query changed; restart from the first page")
        else:
            if self.pending >= 8:
                raise MediaError("search_busy")
            self.pending += 1
            previous = self.latest.get(key)
            try:
                async with self.locks.setdefault(key, asyncio.Lock()):
                    snapshot = self.snapshots.get(self.latest.get(key))
                    if (
                        not snapshot
                        or time.monotonic() - snapshot["created"] > 30
                        # Coalesce only a refresh completed while this caller waited.
                        # Windows clock ticks can be equal across sequential requests.
                        or (refresh and snapshot["id"] == previous)
                    ):
                        results = await asyncio.gather(*(self.query(q, platforms) for q in queries))
                        diagnostics = [
                            {k: v for k, v in result.items() if k != "items"} for result in results
                        ]
                        if all(result["state"] == "failed" for result in results):
                            if self.history:
                                self.history.record(
                                    {
                                        "queries": queries,
                                        "platforms": platforms,
                                        "diagnostics": diagnostics,
                                        "outcome": "search_failed",
                                    }
                                )
                            raise MediaError(results[0]["error"])
                        found = {}
                        for result in results:
                            for item in result.get("items", []):
                                item["matched_queries"] = [result["query"]] if item["title_match"] else []
                                merge_candidate(found, item)
                        items = sorted(
                            found.values(),
                            key=lambda item: (not item["title_match"], not item["acquisition"]["transfer"]),
                        )
                        limited = len(items) > 500 or any(result.get("truncated") for result in results)
                        items = items[:500]
                        health = await self.health()
                        matching = [item for item in items if item["title_match"]]
                        outcome = (
                            "acquirable_candidates"
                            if any(
                                i["platform"] in {a["provider"] for a in self.accounts.values()}
                                for i in matching
                            )
                            else "discovery_only_candidates"
                            if matching
                            else "ambiguous_candidates"
                            if items
                            else "no_candidates_in_observed_results"
                        )
                        # These are read evidence, not judgments that a movie/file is correct.
                        if self.history:
                            for item in items:
                                accounts = [
                                    k for k, a in self.accounts.items() if a["provider"] == item["platform"]
                                ]
                                if len(accounts) == 1:
                                    item["last_inspection"] = self.history.observation(
                                        share_id(item["share_url"]), item["password"], account=accounts[0]
                                    )
                        snapshot = {
                            "id": uuid.uuid4().hex,
                            "key": key,
                            "created": time.monotonic(),
                            "observed_at": time.time(),
                            "items": items,
                            "source_health": health,
                            "diagnostics": diagnostics,
                            "truncated": limited,
                            "outcome": outcome,
                            "counts": {
                                "candidates": len(items),
                                "title_matches": len(matching),
                                "platforms": {p: sum(i["platform"] == p for i in items) for p in platforms},
                            },
                        }
                        if len(self.snapshots) >= 32:
                            del self.snapshots[next(iter(self.snapshots))]
                        self.snapshots[snapshot["id"]] = snapshot
                        self.latest[key] = snapshot["id"]
                        if self.history:
                            self.history.record(
                                {
                                    "queries": queries,
                                    "platforms": platforms,
                                    "search_id": snapshot["id"],
                                    "diagnostics": diagnostics,
                                    "counts": snapshot["counts"],
                                    "outcome": outcome,
                                    "truncated": limited,
                                    "candidates_sample": [
                                        {
                                            k: i[k]
                                            for k in (
                                                "candidate_id",
                                                "share_url",
                                                "platform",
                                                "title",
                                                "title_match",
                                            )
                                        }
                                        for i in items[:10]
                                    ],
                                    "source_health": health,
                                }
                            )
            finally:
                self.pending -= 1
        return {
            "items": snapshot["items"][before : before + limit],
            "search_id": snapshot["id"],
            "next": before + limit if before + limit < len(snapshot["items"]) else None,
            **{
                k: snapshot[k]
                for k in ("observed_at", "source_health", "diagnostics", "counts", "truncated", "outcome")
            },
            "complete": False,
            "refresh_after_seconds": 8,
            "next_actions": next_actions(snapshot),
            "note": "Partial public coverage; title matches are hints, not verified films. "
            "Per-source completion is UNKNOWN: this PanSou API does not expose it. "
            "Health is recent aggregate evidence, not query completion. If needed wait about 8s, "
            "refresh at most twice with identical query/aliases/platforms and no cursor. "
            "Refresh rereads the backend cache, not a forced rescan. Other platforms are discovery-only.",
        }

    async def query(self, query, platforms):
        started = time.monotonic()
        try:
            async with self.slots:
                result = await self.fetch(query, platforms)
            return {
                "query": query,
                "state": "response_received",
                "source_completion": "unknown",
                "elapsed_ms": round((time.monotonic() - started) * 1000),
                **result,
            }
        except MediaError as exc:
            return {
                "query": query,
                "state": "failed",
                "source_completion": "unknown",
                "error": exc.code,
                "elapsed_ms": round((time.monotonic() - started) * 1000),
            }

    async def fetch(self, query, platforms):
        import aiohttp

        try:
            async with self.session.post(
                self.url + "/api/search",
                json={"kw": query, "src": "all", "res": "results", "cloud_types": platforms},
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
        dropped = 0
        truncated = len(results) > 500
        for row in results[:500]:
            if not isinstance(row, dict) or not isinstance(row.get("links"), list):
                dropped += 1
                continue
            truncated |= len(row["links"]) > 30
            for link in row["links"][:30]:
                try:
                    if not isinstance(link, dict):
                        raise ValueError()
                    platform, ident, url, code = public_link(link.get("url"))
                    if platform not in platforms:
                        continue
                    password = link.get("password") or code
                    if not isinstance(password, str) or len(password) > 32:
                        raise ValueError()
                except ValueError:
                    dropped += 1
                    continue
                title = str(link.get("work_title") or link.get("note") or row.get("title") or "")[:300]
                source = str(row.get("channel") or row.get("unique_id") or "")[:160]
                item = {
                    "candidate_id": candidate_id(platform, ident),
                    "platform": platform,
                    "share_url": url,
                    "title": title,
                    "context_title": str(row.get("title") or "")[:300],
                    "password": password,
                    "source": source,
                    "sources": [source] if source else [],
                    "published_at": str(link.get("datetime") or row.get("datetime") or "")[:80],
                    "title_match": title_match(query, title),
                    "verified": False,
                    "acquisition": acquisition(
                        platform, connected=any(a["provider"] == platform for a in self.accounts.values())
                    ),
                    "password_conflict": False,
                }
                merge_candidate(found, item)
        return {
            "items": list(found.values())[:500],
            "candidate_count": min(len(found), 500),
            "rejected_links_or_rows": dropped,
            "truncated": truncated or len(found) > 500,
        }

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
                "observed_at": time.time(),
                "per_query_completion": "unavailable",
                "plugins": inventory(body.get("plugins")),
                "channels": inventory(body.get("channels")),
                "recent_evidence": recent_evidence(live),
                "degraded_plugins": names(live.get("degraded_plugins")),
                "failing_channels": names(live.get("failing_channels")),
                "failing_plugins": names(live.get("failing_plugins")),
                "zero_yield_plugins": names(live.get("zero_yield_plugins")),
                "zero_yield_channels": names(live.get("zero_yield_channels")),
            }
        except (aiohttp.ClientError, OSError, TimeoutError, MediaError, AttributeError):
            return {"state": "unavailable"}


def inventory(value):
    return [item[:80] for item in value[:128] if isinstance(item, str)] if isinstance(value, list) else []


def recent_evidence(live):
    if not isinstance(live, dict):
        return {}
    output = {}
    for group in (
        "failing_plugins",
        "zero_yield_plugins",
        "degraded_plugins",
        "failing_channels",
        "zero_yield_channels",
    ):
        values = live.get(group)
        output[group] = (
            [
                {
                    "name": str(item.get("name") or "")[:80],
                    **{
                        key: item[key]
                        for key in ("rounds", "yielded", "failed", "zero_yield")
                        if type(item.get(key)) is int and item[key] >= 0
                    },
                }
                for item in values[:16]
                if isinstance(item, dict)
            ]
            if isinstance(values, list)
            else []
        )
    return output


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
    return bool(query) and (query in title or bool(len(base) >= 2 and base in title))


def merge_candidate(found, item):
    key = item["candidate_id"]
    old = found.get(key)
    if not old:
        found[key] = item
        return
    winner = item if item["title_match"] and not old["title_match"] else old
    codes = {code for code in (item["password"], old["password"]) if code}
    conflict = old["password_conflict"] or item["password_conflict"] or len(codes) > 1
    found[key] = {
        **winner,
        "sources": list(dict.fromkeys(old["sources"] + item["sources"]))[:16],
        "matched_queries": list(
            dict.fromkeys(old.get("matched_queries", []) + item.get("matched_queries", []))
        )[:4],
        "password_conflict": conflict,
        "password": "" if conflict else next(iter(codes), ""),
    }


def next_actions(snapshot):
    outcome = snapshot["outcome"]
    actions = {
        "acquirable_candidates": ["inspect_actual_files_and_identity"],
        "discovery_only_candidates": [
            "report_provider_not_connected",
            "look_for_authorized_acquirable_alternative",
        ],
        "ambiguous_candidates": [
            "check_title_aliases_and_work_identity",
            "use_public_web_search_if_available",
        ],
        "no_candidates_in_observed_results": [
            "try_short_title_or_verified_alias",
            "use_public_web_search_if_available",
        ],
    }[outcome].copy()
    if any(q["state"] == "failed" for q in snapshot["diagnostics"]):
        actions.append("report_failed_queries_separately")
    if snapshot["source_health"].get("failing_plugins"):
        actions.append("report_recent_source_failures_not_resource_absence")
    return actions
