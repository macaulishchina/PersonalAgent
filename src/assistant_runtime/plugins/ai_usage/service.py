"""Bounded durable admission. One worker owns SQLite, never the asyncio loop."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
import logging
import re
import uuid

from ...kernel.ai_usage import UsageError, UsageScope
from .ledger import Ledger
from .settings import UsageSettings


class UsageService:
    def __init__(self, folder, settings=None):
        self.settings = settings or UsageSettings()
        self.ledger = Ledger(folder / "usage.sqlite3", self.settings)
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ai-usage")
        self.pending = 0
        self.snapshots = {}
        self.blocked_plans = {}
        self.ready = False
        self.error = None
        self.closing = False

    async def start(self):
        try:
            await self.call("open", startup=True)
            self.ready = True
            self.blocked_plans = await self.call("plans")
        except Exception:
            self.error = "usage_storage_unavailable"

    async def call(self, action, *args, startup=False):
        if self.closing or (not startup and not self.ready):
            raise UsageError("usage_unavailable")
        if self.pending >= self.settings.queue_size:
            raise UsageError("usage_busy")
        self.pending += 1
        try:
            # Shield the transaction: cancellation cannot cause an unobserved write
            # to be followed by a second reservation. The dispatch caller never sends
            # after cancellation; a conservative orphan is recovered on restart.
            future = asyncio.get_running_loop().run_in_executor(
                self.executor, getattr(self.ledger, action), *args
            )
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            if action == "admit" and "future" in locals():
                try:
                    # The caller has not received the permit, so no adapter could
                    # have sent it. Finish the reservation and release it durably.
                    ticket = await asyncio.shield(future)
                    await asyncio.get_running_loop().run_in_executor(
                        self.executor,
                        self.ledger.settle,
                        ticket["id"],
                        {"state": "not_sent", "error": "cancelled_before_dispatch"},
                    )
                except Exception:
                    pass  # Recovery retains an unresolved reservation, never replays.
            raise
        except (UsageError, ValueError):
            raise
        except Exception as exc:
            logging.getLogger(__name__).error(
                "ai_usage_storage_failed action=%s type=%s errno=%s",
                action,
                type(exc).__name__,
                getattr(exc, "errno", None),
            )
            self.ready = False
            self.error = "usage_storage_unavailable"
            raise UsageError(self.error) from None
        finally:
            if "future" in locals():

                def completed(work):
                    self.pending -= 1
                    if not work.cancelled():
                        error = work.exception()  # Consume failures after a caller deadline.
                        if error is not None and not isinstance(error, (UsageError, ValueError)):
                            self.ready = False
                            self.error = "usage_storage_unavailable"

                future.add_done_callback(completed)
            else:
                self.pending -= 1

    async def bind(self, ident, scope):
        return UsageScope(**await self.call("bind", ident, scope))

    async def binding(self, ident):
        result = await self.call("binding", ident)
        return UsageScope(**result) if result else None

    async def admit(
        self, scope, *, service, provider, model="", bounds=None, compaction=False, body=None, details=None
    ):
        if (
            not isinstance(scope, UsageScope)
            or any(not isinstance(v, str) or len(v) > 200 for v in asdict(scope).values())
            or not scope.work_id
        ):
            raise UsageError("usage_invalid_identity")
        for value in (service, provider, model):
            if not isinstance(value, str) or len(value) > 200:
                raise UsageError("usage_invalid_dimensions")
        bounds = bounds or {}
        if any(
            k not in {"input_tokens", "output_tokens"} or type(v) is not int or v < 0
            for k, v in bounds.items()
        ):
            raise UsageError("usage_invalid_reservation")
        details = dict(details or {})
        if set(details) - {
            "model_alias",
            "reasoning_effort",
            "max_output_tokens",
            "operation",
            "input_bytes",
        } or any(type(v) not in (str, int, bool) for v in details.values()):
            raise UsageError("usage_invalid_details")
        captured = self.sanitize(body) if self.settings.body_enabled and body is not None else None
        try:
            record = await self.call(
                "admit", scope, service, provider, model, bounds, compaction, captured, details
            )
        except UsageError:
            if self.ready:
                snapshot = await self.snapshot(scope.work_id)
                if snapshot.get("blocked"):
                    self.blocked_plans.update(await self.call("plans"))
            raise
        self.snapshots[scope.work_id] = record.pop("_snapshot")
        if len(self.snapshots) > 256:
            self.snapshots.pop(next(iter(self.snapshots)))
        return record

    def sanitize(self, value):
        import json

        def clean(item, depth=0):
            if depth > 8:
                return "[depth limit]"
            if isinstance(item, dict):
                return {
                    str(k): "[redacted]"
                    if re.search(
                        r"key|token|auth|cookie|password|secret|credential|image|audio|video", str(k), re.I
                    )
                    else clean(v, depth + 1)
                    for k, v in list(item.items())[:100]
                }
            if isinstance(item, list):
                return [clean(v, depth + 1) for v in item[:100]]
            if isinstance(item, str):
                return re.sub(r"(?i)(?:Bearer\s+\S+|sk-[A-Za-z0-9_-]{8,})", "[redacted]", item)[
                    : self.settings.body_max_bytes
                ]
            return item if item is None or type(item) in (int, float, bool) else "[unsupported]"

        text = json.dumps(clean(value), ensure_ascii=False, allow_nan=False)
        return text.encode()[: self.settings.body_max_bytes].decode("utf-8", errors="ignore")

    async def settle(
        self,
        ident,
        *,
        state,
        usage=None,
        http_status=None,
        provider_request_id=None,
        error=None,
        response_body=None,
    ):
        if state not in {"completed", "rejected", "unknown", "not_sent"}:
            raise ValueError("Invalid usage outcome")
        result = {"state": state}
        if isinstance(usage, dict):
            result["usage"] = {
                k: v
                for k, v in usage.items()
                if k in {"input_tokens", "output_tokens", "cached_input_tokens", "reasoning_tokens"}
                and type(v) is int
                and v >= 0
            }
            for field, native, child in (
                ("cached_input_tokens", "input_tokens_details", "cached_tokens"),
                ("reasoning_tokens", "output_tokens_details", "reasoning_tokens"),
            ):
                details = usage.get(native)
                value = details.get(child) if isinstance(details, dict) else None
                if type(value) is int and value >= 0:
                    result["usage"][field] = value
        if type(http_status) is int:
            result["http_status"] = http_status
        if isinstance(provider_request_id, str) and re.fullmatch(
            r"[A-Za-z0-9_.:-]{1,128}", provider_request_id
        ):
            result["provider_request_id"] = provider_request_id
        if isinstance(error, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,200}", error):
            result["error"] = error
        if response_body is not None and self.settings.body_enabled:
            result["response_body"] = self.sanitize(response_body)
        receipt = await self.call("settle", ident, result)
        await self.snapshot(receipt["work_id"])
        logging.getLogger(__name__).info(
            "ai_usage_call id=%s work=%s service=%s provider=%s model=%s state=%s elapsed_ms=%s",
            receipt["id"],
            receipt["work_id"],
            receipt["service"],
            receipt["provider"],
            receipt["model"],
            receipt["state"],
            receipt.get("elapsed_ms"),
        )
        return receipt

    async def snapshot(self, work):
        result = await self.call("snapshot", work)
        self.snapshots[work] = result
        if len(self.snapshots) > 256:
            self.snapshots.pop(next(iter(self.snapshots)))
        return result

    async def close(self):
        if self.closing:
            return
        self.closing = True
        try:
            await asyncio.get_running_loop().run_in_executor(self.executor, self.ledger.close)
        finally:
            self.ready = False
            self.executor.shutdown(wait=False)


async def metered(usage, scope, service, provider, model, invoke, *, bounds=None, body=None):
    """For one already-validated no-tool request, including synthetic adapters."""
    details = {}
    if isinstance(body, dict):
        for key in ("model", "max_output_tokens"):
            if key in body:
                details["model_alias" if key == "model" else key] = body[key]
        if isinstance(body.get("reasoning"), dict) and "effort" in body["reasoning"]:
            details["reasoning_effort"] = body["reasoning"]["effort"]
    ticket = await usage.admit(
        scope,
        service=service,
        provider=provider,
        model=model,
        bounds=bounds,
        body=body,
        details=details,
    )
    try:
        result = await invoke()
    except BaseException as exc:
        status = None
        match = re.fullmatch(r"model_http_(\d{3})", str(exc))
        if match:
            status = int(match[1])
        await usage.settle(
            ticket["id"],
            state="rejected" if status else "unknown",
            http_status=status,
            error=getattr(exc, "code", type(exc).__name__),
        )
        raise
    result["usage_call_id"] = ticket["id"]
    await usage.settle(
        ticket["id"],
        state="completed",
        usage=result.get("usage"),
        provider_request_id=result.get("provider_request_id"),
        response_body=result,
    )
    return result


def independent_scope(service):
    ident = service + ":" + uuid.uuid4().hex
    return UsageScope(ident, ident)
