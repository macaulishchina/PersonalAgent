"""Shared lookup owner: bounded IO, caller checks, metadata receipts, ephemeral cache."""

from .declaration import DECLARATION

import asyncio
from collections import OrderedDict
import copy
import hashlib
import json
import logging
import os
from pathlib import Path
import time
import uuid

import aiohttp
from dotenv import dotenv_values

from assistant_runtime.api.contracts import SearchRequest, TaskRequest
from .deepseek import DeepSeek, fetch_result, query_payload, search_result
from .repository import SearchRepository
from .urls import SearchError, public_url, validate_fetch_url

log = logging.getLogger(__name__)


def credential(root, name):
    value = os.environ.get(name)
    if not value:
        path = Path(root) / ".env"
        if path.is_file():
            if path.stat().st_size > 65536:
                raise SearchError("credential_file_too_large")
            value = dotenv_values(path, interpolate=False).get(name)
    return value if isinstance(value, str) else ""


class SearchPlugin:
    manifest = DECLARATION.manifest

    def __init__(self, api):
        self.api = api
        self.settings = api.config
        self.slots = asyncio.Semaphore(self.settings.concurrency)
        self.pending = 0
        self.flights = {}
        self.cache = OrderedDict()
        self.cache_bytes = 0
        self.accepting = False
        self.last_error = {}
        self.repo = api.repository(SearchRepository)

    async def start(self, context):
        self.context = context
        self.session = aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(limit=self.settings.concurrency),
            timeout=aiohttp.ClientTimeout(total=self.settings.timeout_seconds),
            cookie_jar=aiohttp.DummyCookieJar(),
            trust_env=False,
        )
        context.own(self.session.close)
        clients = {}
        self.providers = {}
        for name, spec in self.settings.providers.items():
            key = self.api.credential(spec["api_key_env"])
            identity = (spec["base_url"], key)
            if identity not in clients:
                clients[identity] = DeepSeek(
                    self.session,
                    spec["base_url"],
                    key,
                    self.settings.requests_per_second,
                    usage=self.api.resource("usage"),
                )
            self.providers[name] = clients[identity]
        self.accepting = True
        context.own(self.close)
        context.register(SearchRequest, self.request)

    async def close(self):
        self.accepting = False
        tasks = [flight["task"] for flight in self.flights.values()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.cache.clear()
        self.cache_bytes = 0

    async def authorize(self, run):
        if run is None:
            return
        if not isinstance(run, str) or not 1 <= len(run) <= 200:
            raise SearchError("invalid_search_caller")
        info = await self.api.invoke(TaskRequest("execution_info", {"run_ids": [run]}))
        if info.get(run, {}).get("state") not in {"running", "waiting_user"}:
            raise SearchError("inactive_search_caller")

    def capabilities(self):
        return {
            "default_provider": self.settings.default_provider,
            "providers": [
                {
                    "id": name,
                    "adapter": spec["adapter"],
                    "capabilities": ["search", "fetch"],
                    "credential_present": bool(self.providers[name].key),
                    "last_error": self.last_error.get(name),
                    "validation": "configuration_only",
                }
                for name, spec in self.settings.providers.items()
            ],
            "limits": {
                "query_characters_with_filters": 128,
                "count": 50,
                "default_content_chars": 20000,
                "max_content_chars": 60000,
                "response_bytes": 2 * 1024 * 1024,
                "concurrency": self.settings.concurrency,
                "max_pending": self.settings.max_pending,
                "timeout_seconds": self.settings.timeout_seconds,
                "cache_seconds": self.settings.cache_seconds,
                "requests_per_second": self.settings.requests_per_second,
            },
            "pending": self.pending,
            "inflight": len(self.flights),
            "cache_entries": len(self.cache),
            "cache_bytes": self.cache_bytes,
            "automatic_provider_fallback": False,
            "note": "Configured is not remotely verified. Subscription Search Service remains a separate tool; choose either as appropriate. No model is invoked here.",
        }

    def record(self, receipt):
        # Only bounded operational metadata, never queries, URLs, page bodies, keys or upstream errors.
        raw = json.dumps(receipt, ensure_ascii=False)
        self.repo.record(raw)
        log.info("search_call %s", raw)

    def remember(self, key, result):
        size = len(json.dumps(result, ensure_ascii=False).encode())
        if not self.settings.cache_seconds or size > 16 * 1024 * 1024:
            return
        self.cache[key] = (time.monotonic(), result, size)
        self.cache_bytes += size
        while len(self.cache) > 128 or self.cache_bytes > 16 * 1024 * 1024:
            _, (_, _, removed) = self.cache.popitem(last=False)
            self.cache_bytes -= removed

    def forget(self, key):
        item = self.cache.pop(key, None)
        if item:
            self.cache_bytes -= item[2]

    async def work(self, provider, action, payload, maximum, deadline, metrics, scope):
        try:
            async with asyncio.timeout_at(deadline):
                async with self.slots:
                    if action == "fetch":
                        await validate_fetch_url(payload["url"])
                    body = await self.providers[provider].call(
                        action, payload, metrics, deadline, scope=scope, provider=provider
                    )
                    result = (
                        search_result(body, payload["count"])
                        if action == "search"
                        else fetch_result(body, payload["url"], maximum)
                    )
                    self.last_error.pop(provider, None)
                    return {"state": "completed", "observed_at": time.time(), **result, **metrics}
        except TimeoutError:
            code = "search_timeout"
        except SearchError as exc:
            code = exc.code
        self.last_error[provider] = code
        return {"state": "failed", "error": {"code": code}, **metrics}

    async def lookup(self, provider, action, payload, maximum, refresh, deadline, trace, scope):
        key = hashlib.sha256(
            json.dumps([provider, action, payload, maximum], sort_keys=True).encode()
        ).hexdigest()
        now = time.monotonic()
        for old, item in list(self.cache.items()):
            if now - item[0] >= self.settings.cache_seconds:
                self.forget(old)
        if refresh:
            self.forget(key)
        if key in self.cache:
            self.cache.move_to_end(key)
            return {**copy.deepcopy(self.cache[key][1]), "cached": True, "coalesced": False}
        flight = self.flights.get(key)
        coalesced = flight is not None
        if flight is None:
            metrics = {"upstream_request_id": uuid.uuid4().hex, "upstream_attempts": 0}
            flight = {
                "metrics": metrics,
                "task": self.context.spawn(
                    self.work(provider, action, payload, maximum, deadline, metrics, scope), "lookup"
                ),
                "waiters": 0,
            }
            self.flights[key] = flight
        flight["waiters"] += 1
        try:
            result = await asyncio.shield(flight["task"])
            if result["state"] == "completed" and key not in self.cache and self.accepting:
                self.remember(key, result)
            return {**copy.deepcopy(result), "cached": False, "coalesced": coalesced}
        finally:
            trace.update(flight["metrics"])
            trace["coalesced"] = coalesced
            flight["waiters"] -= 1
            if not flight["waiters"]:
                if not flight["task"].done():
                    flight["task"].cancel()
                    await asyncio.gather(flight["task"], return_exceptions=True)
                if self.flights.get(key) is flight:
                    del self.flights[key]

    async def request(self, request):
        if not self.accepting:
            raise SearchError("search_stopping")
        if self.pending >= self.settings.max_pending:
            raise SearchError("search_busy")
        self.pending += 1
        started = time.monotonic()
        deadline = started + self.settings.timeout_seconds
        ident = uuid.uuid4().hex
        provider = None
        trace = {}
        result = {"state": "failed", "error": {"code": "invalid_search_request"}}
        action, args = request.action, request.arguments
        try:
            async with asyncio.timeout_at(deadline):
                await self.authorize(request.caller_run)
                fields = {
                    "capabilities": set(),
                    "history": {"before", "limit"},
                    "search": {
                        "provider",
                        "query",
                        "count",
                        "allowed_domains",
                        "blocked_domains",
                        "languages",
                        "after",
                        "before",
                        "refresh",
                    },
                    "fetch": {"provider", "url", "max_chars", "refresh"},
                }
                if (
                    not isinstance(action, str)
                    or action not in fields
                    or not isinstance(args, dict)
                    or set(args) - fields[action]
                ):
                    raise SearchError("invalid_search_arguments")
                if action == "capabilities":
                    return self.capabilities()
                if action == "history":
                    before, limit = args.get("before"), args.get("limit", 20)
                    if (
                        type(limit) is not int
                        or not 1 <= limit <= 100
                        or (before is not None and (type(before) is not int or before < 1))
                    ):
                        raise SearchError("invalid_search_history_page")
                    return self.repo.history(before, limit)
                provider = args.get("provider") or self.settings.default_provider
                if not isinstance(provider, str) or provider not in self.providers:
                    provider = None
                    raise SearchError("unknown_search_provider")
                refresh = args.get("refresh", False)
                if type(refresh) is not bool:
                    raise SearchError("invalid_search_refresh")
                maximum = 0
                if action == "search":
                    payload = query_payload(args)
                else:
                    maximum = args.get("max_chars", 20000)
                    if type(maximum) is not int or not 1 <= maximum <= 60000:
                        raise SearchError("invalid_page_content_limit")
                    payload = {"url": public_url(args.get("url"))}
                scope = await self.api.resource("usage").scope(request.caller_run)
                result = await self.lookup(
                    provider, action, payload, maximum, refresh, deadline, trace, scope
                )
                # A stopped execution may not receive newly fetched/cached information.
                await self.authorize(request.caller_run)
        except TimeoutError:
            result = {"state": "failed", "error": {"code": "search_timeout"}}
        except SearchError as exc:
            result = {"state": "failed", "error": {"code": exc.code}}
        except asyncio.CancelledError:
            result = {"state": "cancelled"}
            raise
        finally:
            self.pending -= 1
            if isinstance(action, str) and action in {"search", "fetch"}:
                receipt = {
                    "request_id": ident,
                    "caller": request.caller_run
                    if isinstance(request.caller_run, str) and len(request.caller_run) <= 200
                    else "foreground",
                    "provider": provider,
                    "action": action,
                    "created_at": time.time(),
                    "elapsed_ms": round((time.monotonic() - started) * 1000),
                    **trace,
                    **{
                        k: result[k]
                        for k in (
                            "state",
                            "error",
                            "cached",
                            "coalesced",
                            "upstream_request_id",
                            "upstream_attempts",
                            "provider_request_id",
                        )
                        if k in result
                    },
                }
                self.record(receipt)
        return {
            **trace,
            **result,
            "request_id": ident,
            "provider": provider,
            "elapsed_ms": round((time.monotonic() - started) * 1000),
        }
