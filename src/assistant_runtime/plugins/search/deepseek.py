"""DeepSeek Search/Fetch wire protocol; bounded reads, no model call or redirects."""

import asyncio
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
import json
import re
import time

import aiohttp

from .urls import SearchError, public_url


def query_payload(args):
    query = args.get("query")
    count = args.get("count", 10)
    if (
        not isinstance(query, str)
        or not 1 <= len(query.strip()) <= 128
        or type(count) is not int
        or not 1 <= count <= 50
    ):
        raise SearchError("invalid_search_query_or_count")
    try:
        query.encode("utf-8")
    except UnicodeError:
        raise SearchError("invalid_search_query_or_count") from None
    parts = [query.strip()]
    domains = {}
    for key, prefix in (("allowed_domains", "site:"), ("blocked_domains", "-site:")):
        values = args.get(key, [])
        if not isinstance(values, list) or len(values) > 10:
            raise SearchError("invalid_search_domains")
        normalized = []
        for value in values:
            if (
                not isinstance(value, str)
                or not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?", value)
                or ".." in value
                or "." not in value
            ):
                raise SearchError("invalid_search_domains")
            normalized.append(value.lower())
        domains[key] = normalized
        if normalized:
            parts.append((" OR " if key == "allowed_domains" else " ").join(prefix + v for v in normalized))
    if set(domains["allowed_domains"]) & set(domains["blocked_domains"]):
        raise SearchError("conflicting_search_domains")
    languages = args.get("languages", [])
    if (
        not isinstance(languages, list)
        or len(languages) > 5
        or any(
            not isinstance(v, str) or not re.fullmatch(r"[a-z]{2,3}(?:-[A-Za-z]{2,4})?", v) for v in languages
        )
    ):
        raise SearchError("invalid_search_languages")
    if languages:
        parts.append("lang:" + ",".join(languages))
    dates = {}
    for key in ("after", "before"):
        value = args.get(key)
        if value is not None:
            try:
                if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                    raise ValueError()
                dates[key] = date.fromisoformat(value)
            except ValueError:
                raise SearchError("invalid_search_date") from None
            parts.append(key + ":" + value)
    if "after" in dates and "before" in dates and dates["after"] > dates["before"]:
        raise SearchError("invalid_search_date_range")
    text = " ".join(parts)
    if len(text) > 128:
        raise SearchError("search_query_with_filters_exceeds_128_characters")
    return {"query": text, "count": count, "need_content": False}


def retry_delay(value):
    try:
        delay = float(value)
    except (ValueError, TypeError):
        try:
            delay = (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds()
        except (ValueError, TypeError, OverflowError):
            delay = 0.25
    return min(120, max(0.05, delay))


class DeepSeek:
    def __init__(self, session, endpoint, key, requests_per_second=5, *, usage):
        self.session, self.endpoint, self.key = session, endpoint, key
        self.usage = usage
        self.interval = 1 / requests_per_second
        self.rate_lock = asyncio.Lock()
        self.next_request = 0.0

    async def call(self, action, payload, metrics, deadline, *, scope, provider):
        if not self.key:
            raise SearchError("credentials_missing")
        if len(self.key) > 4096 or any(ord(c) <= 32 or ord(c) >= 127 for c in self.key):
            raise SearchError("credentials_invalid")
        for attempt in range(2):
            async with self.rate_lock:
                await asyncio.sleep(max(0, self.next_request - time.monotonic()))
                self.next_request = time.monotonic() + self.interval
            from assistant_runtime.api.ai_usage import UsageError

            try:
                ticket = await self.usage.admit(
                    scope, service="search", provider=provider, model=action, body=payload
                )
            except UsageError as exc:
                raise SearchError(exc.code) from None
            metrics["upstream_attempts"] += 1
            metrics.setdefault("usage_call_ids", []).append(ticket["id"])
            confirmed = False
            http_status = None
            delay = 0.25
            try:
                async with self.session.post(
                    self.endpoint + "/v1/tools/" + action,
                    json=payload,
                    headers={"Authorization": "Bearer " + self.key},
                    allow_redirects=False,
                ) as response:
                    confirmed = True
                    http_status = response.status
                    ident = response.headers.get("x-request-id", "")
                    if re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", ident):
                        metrics["provider_request_id"] = ident
                    if response.status == 200:
                        raw = bytearray()
                        async for chunk in response.content.iter_chunked(65536):
                            raw.extend(chunk)
                            if len(raw) > 2 * 1024 * 1024:
                                raise SearchError("search_response_too_large")
                        try:
                            body = json.loads(raw)
                            if not isinstance(body, dict):
                                raise ValueError()
                            return body
                        except ValueError:
                            raise SearchError("invalid_search_response") from None
                    code = {
                        400: "provider_request_rejected",
                        401: "authentication_failed",
                        402: "insufficient_balance",
                        403: "provider_access_denied",
                        404: "page_unavailable" if action == "fetch" else "provider_endpoint_missing",
                        408: "provider_timeout",
                        413: "provider_request_too_large",
                        429: "rate_limited",
                    }.get(
                        response.status,
                        "provider_unavailable" if response.status >= 500 else "unexpected_provider_status",
                    )
                    if response.status not in {408, 429} and response.status < 500:
                        raise SearchError(code)
                    delay = retry_delay(response.headers.get("Retry-After"))
            except (aiohttp.ClientError, OSError, TimeoutError):
                code = "provider_network_error"
            finally:
                await self.usage.settle(
                    ticket["id"],
                    state="completed" if http_status == 200 else "rejected" if confirmed else "unknown",
                    http_status=http_status,
                    provider_request_id=metrics.get("provider_request_id"),
                )
            if attempt or time.monotonic() + delay >= deadline:
                raise SearchError(code)
            await asyncio.sleep(delay)
        raise SearchError("provider_unavailable")


def text(value, limit):
    if not isinstance(value, str):
        raise SearchError("invalid_search_response")
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise SearchError("invalid_search_response") from None
    return value[:limit]


def search_result(body, count):
    rows = body.get("results")
    if not isinstance(rows, list):
        raise SearchError("invalid_search_response")
    results = []
    rejected = 0
    truncated = len(rows) > count
    for row in rows[:count]:
        if not isinstance(row, dict):
            raise SearchError("invalid_search_response")
        try:
            url = public_url(row.get("url"))
        except SearchError:
            rejected += 1
            continue
        item = {"url": url}
        for key, limit in (("title", 500), ("snippet", 1000), ("summary", 4000)):
            value = row.get(key, "")
            item[key] = text(value, limit)
            truncated |= len(value) > limit
        for key, maximum in (("site_name", 300), ("date", 80)):
            value = row.get(key)
            item[key] = text(value, maximum) if isinstance(value, str) else None
        results.append(item)
    return {
        "results": results,
        "empty": not rows,
        "rejected_results": rejected,
        "truncated": truncated,
        "coverage": "provider_results_not_exhaustive",
    }


def fetch_result(body, url, maximum):
    value = body.get("content")
    if not isinstance(value, str) or not value.strip():
        raise SearchError("page_content_unavailable")
    return {
        "url": url,
        "final_url": None,
        "title": text(body.get("title", ""), 500),
        "content": text(value, maximum),
        "content_chars": len(value),
        "truncated": len(value) > maximum,
        "format": "markdown",
    }
