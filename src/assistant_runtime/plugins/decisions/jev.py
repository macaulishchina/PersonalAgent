"""TypeSafe HTTP adapter. Secrets, native question fields and errors stay here."""

from assistant_runtime.api.configuration_io import configuration_path

import asyncio
import json
import math
import os
from pathlib import Path
import time

import aiohttp
from dotenv import dotenv_values
from .errors import DecisionError
from .questions import execution_questions


def credential(root):
    key = os.environ.get("TYPESAFE_API_KEY")
    if not key:
        path = configuration_path(Path(root) / ".env")
        if path.is_file():
            if path.stat().st_size > 65536:
                raise DecisionError("credential_file_too_large")
            key = dotenv_values(configuration_path(path), interpolate=False).get("TYPESAFE_API_KEY")
    return normalize_credential(key)


def normalize_credential(key):
    if not isinstance(key, str) or not key.strip():
        raise DecisionError("credential_missing")
    key = key.strip()
    if len(key) > 4096 or any(c.isspace() for c in key):
        raise DecisionError("credential_invalid")
    return key


def answer(payload, criteria, requested):
    try:
        model = payload["model"]
        item = payload["answers"]["decision"]
        values = item["probabilities"]
        confidence = item["confidence"]
        choice = item["choice"]
        if (
            not isinstance(model, str)
            or not model.startswith("jev-")
            or len(model) > 80
            or requested not in {"jev-latest", "jev-preview"}
            and model != requested
            or item["type"] != "choice"
            or choice not in criteria
            or set(values) != set(criteria)
            or type(confidence) not in (int, float)
            or not math.isfinite(confidence)
            or not 0 <= confidence <= 1
            or any(
                type(v) not in (int, float) or not math.isfinite(v) or not 0 <= v <= 1
                for v in values.values()
            )
            or abs(sum(values.values()) - 1) > 0.02
            or values[choice] < max(values.values()) - 0.02
        ):
            raise ValueError()
        usage = payload.get("usage", {})
        if not isinstance(usage, dict) or any(
            type(usage.get(k, 0)) is not int or not 0 <= usage.get(k, 0) <= 1000000
            for k in ("input_tokens", "output_tokens")
        ):
            raise ValueError()
        return {
            "model": model,
            "choice": choice,
            "confidence": confidence,
            "probabilities": values,
            "usage": {k: usage[k] for k in ("input_tokens", "output_tokens") if k in usage},
        }
    except (KeyError, TypeError, ValueError, AttributeError):
        raise DecisionError("invalid_response") from None


class JevProvider:
    endpoint = "https://api.typesafe.ai/v1/systemone"
    models_endpoint = "https://api.typesafe.ai/v1/models"

    def __init__(self, root, settings, *, credential_source=None):
        self.root, self.settings = root, settings
        self.credential_source = credential_source
        self.key = None
        self.session = None
        self.timings = {}
        self.warmup_result = {"state": "not_started" if settings.warmup_enabled else "disabled"}

    def reload(self):
        self.key = None
        try:
            self.key = (
                normalize_credential(self.credential_source())
                if self.credential_source
                else credential(self.root)
            )
        except (OSError, UnicodeError):
            raise DecisionError("credential_unavailable") from None

    async def start(self):
        trace = aiohttp.TraceConfig()

        def mark(name):
            async def record(session, context, params):
                context.trace_request_ctx[name] = time.monotonic()

            return record

        for signal, name in (
            (trace.on_dns_resolvehost_start, "dns_start"),
            (trace.on_dns_resolvehost_end, "dns_end"),
            (trace.on_connection_create_start, "connect_start"),
            (trace.on_connection_create_end, "connect_end"),
            (trace.on_connection_reuseconn, "reused"),
            (trace.on_request_headers_sent, "sent"),
            (trace.on_request_end, "received"),
        ):
            signal.append(mark(name))
        self.session = aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(
                keepalive_timeout=self.settings.keepalive_seconds,
                ttl_dns_cache=self.settings.dns_cache_seconds,
                limit=self.settings.concurrency + 1,
                limit_per_host=self.settings.concurrency + 1,
            ),
            timeout=aiohttp.ClientTimeout(total=self.settings.timeout_seconds),
            trust_env=True,
            trace_configs=[trace],
        )

    async def close(self):
        self.key = None
        if self.session:
            await self.session.close()

    def diagnostics(self):
        return {
            "keepalive_seconds": self.settings.keepalive_seconds,
            "dns_cache_seconds": self.settings.dns_cache_seconds,
            "warmup": dict(self.warmup_result),
            "last_judgment": self.timings.get("judgment"),
        }

    async def warmup(self):
        # One metadata GET per process, never model inference or a recurring timer.
        # It shares the pool, but not the judgment lock or its single worker slot.
        if not self.settings.warmup_enabled or self.warmup_result["state"] != "not_started":
            return
        if not self.settings.enabled or not self.settings.external_data_consent or not self.key:
            self.warmup_result = {"state": "skipped"}
            return
        self.warmup_result = {"state": "running"}
        try:
            await self._request(
                "GET",
                self.models_endpoint,
                "warmup",
                timeout=aiohttp.ClientTimeout(total=min(3, self.settings.timeout_seconds)),
            )
        except asyncio.CancelledError:
            self.warmup_result = {"state": "cancelled", **self.timings.get("warmup", {})}
            raise
        except DecisionError as exc:
            # Metadata availability cannot suspend or back off real judgments.
            self.warmup_result = {"state": "failed", "error": str(exc), **self.timings.get("warmup", {})}
        else:
            self.warmup_result = {"state": "completed", **self.timings["warmup"]}

    async def _request(self, method, url, kind, **kwargs):
        stamps = {}
        started = time.monotonic()
        try:
            if not self.key:
                raise DecisionError("credential_missing")
            async with self.session.request(
                method,
                url,
                headers={"Authorization": "Bearer " + self.key},
                allow_redirects=False,
                trace_request_ctx=stamps,
                **kwargs,
            ) as response:
                if response.status in {401, 403}:
                    raise DecisionError("authentication_failed")
                if response.status == 402:
                    raise DecisionError("quota_exhausted")
                if response.status == 429:
                    raise DecisionError("rate_limited")
                if response.status != 200:
                    raise DecisionError("upstream_unavailable")
                body = bytearray()
                async for chunk in response.content.iter_chunked(8192):
                    body.extend(chunk)
                    if len(body) > 65536:
                        raise DecisionError("response_too_large")
                return body
        except DecisionError:
            raise
        except TimeoutError:
            raise DecisionError("timeout") from None
        except Exception:
            # Provider bodies, URLs, exception strings and tokens never enter logs.
            raise DecisionError("transport_failed") from None
        finally:

            def elapsed(before, after):
                return (
                    round((stamps[after] - stamps[before]) * 1000)
                    if before in stamps and after in stamps
                    else None
                )

            self.timings[kind] = {
                "total_ms": round((time.monotonic() - started) * 1000),
                "reused_connection": "reused" in stamps,
                "dns_ms": elapsed("dns_start", "dns_end"),
                "connection_ms": elapsed("connect_start", "connect_end"),
                "response_headers_ms": elapsed("sent", "received"),
            }

    async def evaluate(self, job):
        questions = (
            execution_questions(job) if job["kind"] == "model_profile" else {"decision": job["question"]}
        )
        try:
            body = await self._request(
                "POST",
                self.endpoint,
                "judgment",
                json={
                    "model": self.settings.model,
                    "state": job["input"],
                    "questions": questions,
                },
            )
        finally:
            if "judgment" in self.timings:
                self.timings["judgment"]["receipt_id"] = job["id"]
        try:
            payload = json.loads(body)
        except (ValueError, UnicodeError):
            raise DecisionError("invalid_response") from None
        if job["kind"] != "model_profile":
            return answer(payload, job["question"]["criteria"], self.settings.model)
        fields = {}
        for native, field in (("profile", "profile"), ("effort", "reasoning_effort")):
            try:
                item = payload["answers"][native]
            except (KeyError, TypeError):
                raise DecisionError("invalid_response") from None
            fields[field] = answer(
                {**payload, "answers": {"decision": item}}, questions[native]["criteria"], self.settings.model
            )
        choice = fields["profile"]["choice"] + "_" + fields["reasoning_effort"]["choice"]
        usage = fields["profile"]["usage"]
        for field in fields.values():
            field.pop("usage")
        return {
            "model": fields["profile"]["model"],
            "choice": choice if choice in job["input"]["options"] else "uncertain",
            "fields": fields,
            "usage": usage,
        }
