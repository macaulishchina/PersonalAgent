"""Bounded Responses transport. No automatic retry, routing or tool execution."""

import json
import math
import time
from assistant_runtime.api.models import ModelCallError


def timing_header(headers, name):
    """Gateway timing is optional telemetry, never a success or billing receipt."""
    try:
        value = float(headers[name])
        return value if math.isfinite(value) and value >= 0 else None
    except (KeyError, TypeError, ValueError):
        return None


class GatewayClient:
    def __init__(self, settings, key, *, accounts=None):
        self.settings, self.key, self.session = settings, key, None
        self.accounts = accounts

    async def start(self):
        import aiohttp

        self.session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=self.settings.timeout_seconds),
            connector=aiohttp.TCPConnector(limit=self.settings.concurrency, keepalive_timeout=60),
            trust_env=False,
        )

    async def close(self):
        if self.accounts:
            await self.accounts.close()
        if self.session:
            await self.session.close()

    async def health(self):
        async with self.session.get(
            self.settings.gateway_url + "/models", headers=self.headers(), allow_redirects=False
        ) as r:
            return {"reachable": r.status == 200, "http_status": r.status}

    def headers(self, session_id=None):
        headers = {"Authorization": "Bearer " + self.key}
        if session_id:
            headers["x-assistant-session"] = session_id
        return headers

    async def complete(self, payload, *, session_id=None):
        entry = self.settings.entries.get(payload.get("model"))
        if entry and self.settings.providers[entry["provider"]]["protocol"] == "pi":
            from assistant_runtime.core.pi.inference import complete

            return await complete(self.accounts, payload, session_id)
        started, first = time.monotonic(), None
        async with self.session.post(
            self.settings.gateway_url + "/responses",
            json={**payload, "stream": True},
            headers=self.headers(session_id),
            allow_redirects=False,
        ) as r:
            if r.status != 200:
                # Response bodies can contain credentials or supplied context.
                raise ModelCallError(
                    "model_http_" + str(r.status),
                    fallback_allowed=r.status in {401, 402, 403, 404, 408, 429, 500, 502, 503, 504},
                )
            total, completed = 0, None
            if "text/event-stream" not in r.headers.get("Content-Type", ""):
                chunks = []
                async for raw in r.content.iter_chunked(65536):
                    total += len(raw)
                    if total > 2 * 1024 * 1024:
                        raise ValueError("Model response too large")
                    chunks.append(raw)
                completed = json.loads(b"".join(chunks))
            else:
                async for raw in r.content:
                    total += len(raw)
                    if total > 2 * 1024 * 1024:
                        raise ValueError("Model stream too large")
                    if not raw.startswith(b"data:") or raw[5:].strip() == b"[DONE]":
                        continue
                    event = json.loads(raw[5:])
                    if event.get("type") == "response.output_text.delta" and first is None:
                        first = time.monotonic() - started
                    if event.get("type") in {"error", "response.failed", "response.incomplete"}:
                        raise RuntimeError("Model response failed or incomplete")
                    if event.get("type") == "response.completed":
                        completed = event["response"]
            if not isinstance(completed, dict) or completed.get("status") != "completed":
                raise RuntimeError("Model stream ended without confirmed completion")
            output = completed.get("output", [])
            if any(item.get("type") not in {"message", "reasoning"} for item in output):
                raise ValueError("Auxiliary inference cannot request or execute tools")
            text = "".join(
                part.get("text", "")
                for item in output
                if item.get("type") == "message"
                for part in item.get("content", [])
                if part.get("type") == "output_text"
            )
            if not text or len(text) > 128000:
                raise ValueError("Missing or oversized model text")
            return {
                "text": text,
                "provider_request_id": completed.get("id"),
                "usage": completed.get("usage"),
                "elapsed_ms": round((time.monotonic() - started) * 1000),
                "first_text_ms": round(first * 1000) if first is not None else None,
                "gateway_overhead_ms": timing_header(r.headers, "x-litellm-overhead-duration-ms"),
                "gateway_response_ms": timing_header(r.headers, "x-litellm-response-duration-ms"),
            }

    async def output_bound(self, payload):
        entry = self.settings.entries.get(payload["model"])
        if entry and self.settings.providers[entry["provider"]]["protocol"] == "pi":
            from assistant_runtime.core.pi.inference import output_bound

            return await output_bound(self.accounts, payload)
        return payload["max_output_tokens"]
