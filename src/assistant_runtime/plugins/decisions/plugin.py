"""Bounded optional suggestions with explicit fallbacks; never executes actions."""

import asyncio
from collections import Counter
import re
import time

from ...kernel.contracts import DecisionRequest, DecisionSample, DecisionQuery, TaskRequest
from ...kernel.decisions import fallback
from ...kernel.runtime import Manifest
from .errors import DecisionError
from .questions import prepare
from .repository import Receipts
from .settings import DecisionSettings


class DecisionsPlugin:
    manifest = Manifest("decisions")

    def __init__(self, host, provider=None):
        self.settings = DecisionSettings.parse(host.config.decisions)
        self.root = host.config.root
        self.provider = provider
        self.receipts = Receipts(host.config.state_dir / "decisions", self.settings)
        self.queue = asyncio.Queue(maxsize=self.settings.queue_size)
        self.counts = Counter()
        self.error = None
        self.auth_blocked = True
        self.retry_at = 0
        self.failures = 0
        self.context = None
        self.provider_lock = asyncio.Lock()
        self.waiters = {}
        self.working = False

    async def start(self, context):
        self.context = context
        context.own(self.receipts.close)
        await self.receipts.call("open")
        if self.settings.enabled:
            try:
                if self.provider is None:
                    from .jev import JevProvider

                    self.provider = JevProvider(self.root, self.settings)
                context.own(self.provider.close)
                await self.provider.start()
                await self.reload()
            except Exception:
                self.error = "provider_initialization_failed"
        self.events = context.runtime.subscribe("decision-shadow", capacity=64)

        async def unsubscribe():
            context.runtime.unsubscribe("decision-shadow")

        context.own(unsubscribe)
        context.register(DecisionRequest, self.request)
        context.register(DecisionQuery, self.judge)
        context.spawn(self.listen(), "samples")
        context.spawn(self.worker(), "evaluate")
        if (
            self.settings.enabled
            and self.settings.external_data_consent
            and self.settings.warmup_enabled
            and not self.auth_blocked
        ):
            context.spawn(self.warmup(), "warmup")

    async def warmup(self):
        # Other plugin startup can occupy the loop/default executor. Begin the
        # network budget only after their startup phase has finished.
        await self.context.runtime.ready.wait()
        await self.provider.warmup()

    async def reload(self):
        if not self.settings.enabled or self.provider is None:
            return
        async with self.provider_lock:
            work = asyncio.create_task(asyncio.to_thread(self.provider.reload))
            try:
                await asyncio.shield(work)
                self.error = None
                self.auth_blocked = False
                self.retry_at = 0
                self.failures = 0
            except asyncio.CancelledError:
                await asyncio.gather(work, return_exceptions=True)
                raise
            except DecisionError as exc:
                self.error = str(exc)
                self.auth_blocked = True

    def enqueue(self, sample, source):
        if not self.settings.enabled:
            return {"accepted": False, "reason": "disabled"}
        if not self.settings.external_data_consent:
            return {"accepted": False, "reason": "external_data_not_authorized"}
        if self.queue.full():
            self.counts["queue_dropped"] += 1
            return {"accepted": False, "reason": "queue_full"}
        try:
            job = prepare(sample, source, self.settings.model)
        except (ValueError, TypeError, RecursionError):
            self.counts["invalid"] += 1
            return {"accepted": False, "reason": "invalid_or_oversized_sample"}
        self.queue.put_nowait(job)
        self.counts["queued"] += 1
        return {
            "accepted": True,
            "id": job["id"],
            "durability": "volatile_queue",
            "mode": self.settings.mode,
            "evaluation_only": True,
        }

    async def listen(self):
        while True:
            event = await self.events.get()
            try:
                if (
                    self.settings.enabled
                    and self.settings.mode == "shadow"
                    and event.kind == "decision.observed"
                    and event.source in {"assistant", "tasks"}
                ):
                    self.enqueue(event.data.get("sample"), event.source)
            finally:
                self.events.task_done()

    async def worker(self):
        while True:
            job = await self.queue.get()
            self.working = True
            claimed = False
            try:
                age = time.time() - job["observed_at"]
                skip = (
                    "expired"
                    if age > self.settings.max_age_seconds
                    or age < -5
                    or job.get("deadline", float("inf")) <= time.monotonic()
                    else "credential_unavailable"
                    if self.auth_blocked
                    else "backoff_skipped"
                    if time.monotonic() < self.retry_at
                    else None
                )
                if not await self.receipts.call("claim", job, skip):
                    continue
                claimed = True
                started = time.monotonic()
                try:
                    async with self.provider_lock:
                        remaining = (
                            job.get("deadline", time.monotonic() + self.settings.timeout_seconds)
                            - time.monotonic()
                        )
                        try:
                            async with asyncio.timeout(max(0.001, remaining)):
                                result = await self.provider.evaluate(job)
                        except TimeoutError:
                            raise DecisionError("timeout") from None
                except asyncio.CancelledError:
                    await self.receipts.call(
                        "finish", job["id"], "unknown", {"error": "shutdown_during_request"}
                    )
                    raise
                except DecisionError as exc:
                    self.error = str(exc)
                    self.failures += 1
                    if str(exc) in {"authentication_failed", "credential_missing", "credential_invalid"}:
                        self.auth_blocked = True
                    else:
                        self.retry_at = time.monotonic() + min(900, 5 * 2 ** min(self.failures, 8))
                    await self.receipts.call(
                        "finish",
                        job["id"],
                        "error",
                        {"error": str(exc), "latency_ms": round((time.monotonic() - started) * 1000)},
                    )
                else:
                    self.error, self.failures = None, 0
                    await self.receipts.call(
                        "finish",
                        job["id"],
                        "completed",
                        {"answer": result, "latency_ms": round((time.monotonic() - started) * 1000)},
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                # Storage or adapter failure degrades evaluation only. Never log
                # arbitrary exception strings, nor retry an uncertain request.
                self.error = "decision_worker_failed"
                self.counts["worker_failed"] += 1
                self.retry_at = time.monotonic() + 60
                if claimed:
                    try:
                        await self.receipts.call("finish", job["id"], "unknown", {"error": self.error})
                    except Exception:
                        pass  # On restart, any outstanding intent becomes unknown.
            finally:
                future = self.waiters.pop(job["id"], None)
                if future is not None and not future.done():
                    try:
                        future.set_result(await self.receipts.call("find", job["id"]))
                    except Exception:
                        future.set_result(None)
                self.working = False
                self.queue.task_done()

    def result(self, row):
        if row is None:
            return fallback("receipt_unavailable")
        if row["state"] != "completed":
            return fallback(row.get("error", row["state"]), row["id"])
        answer = row["answer"]
        choice = answer["choice"]
        threshold = self.settings.thresholds[row["kind"]]
        if row["kind"] == "model_profile":
            fields = {
                key: value["choice"]
                if value["choice"] != "uncertain" and value["probabilities"][value["choice"]] >= threshold
                else None
                for key, value in answer["fields"].items()
            }
            if not any(fields.values()):
                return fallback("low_confidence", row["id"])
            return {
                "status": "ok",
                "choice": choice,
                "field_choices": fields,
                "field_probabilities": {
                    key: value["probabilities"] for key, value in answer["fields"].items()
                },
                "receipt_id": row["id"],
                "model": answer["model"],
                "probability_threshold": threshold,
                "confidence_scope": "per_field",
            }
        if choice == "uncertain" or answer["probabilities"].get(choice, 0) < threshold:
            return fallback("low_confidence", row["id"])
        return {
            "status": "ok",
            "choice": choice,
            "receipt_id": row["id"],
            "model": answer["model"],
            "probabilities": answer["probabilities"],
            "confidence": answer["confidence"],
            "probability_threshold": threshold,
        }

    async def judge(self, request):
        if not self.settings.enabled:
            return fallback("disabled")
        # Every NEW active route is disabled until explicit data/decision consent.
        # No active request reaches preparation, persistence or HTTP before it.
        if not self.settings.external_data_consent:
            return fallback("external_data_not_authorized")
        if self.settings.mode != "active":
            return fallback("shadow_mode")
        if self.auth_blocked:
            return fallback("credential_unavailable")
        if time.monotonic() < self.retry_at:
            return fallback("provider_backoff")
        deadline = time.monotonic() + self.settings.active_timeout_seconds
        try:
            async with asyncio.timeout_at(deadline):
                if request.caller_run is not None:
                    info = await self.context.runtime.invoke(
                        TaskRequest("execution_info", {"run_ids": [request.caller_run]})
                    )
                    if info.get(request.caller_run, {}).get("state") not in {"running", "waiting_user"}:
                        return fallback("caller_not_active")
                source = {
                    "voice_intent": "assistant",
                    "event_relevance": "tasks",
                    "model_profile": "tasks",
                }.get(request.kind, "mcp:background" if request.caller_run else "mcp:foreground")
                reference = (request.caller_run + ":" if request.caller_run else "") + request.reference
                job = prepare(
                    DecisionSample(request.kind, reference, request.state), source, self.settings.model
                )
                row = await self.receipts.call("find", job["id"])
                future = self.waiters.get(job["id"])
                if row and future is None:
                    return self.result(row)
                if future is None:
                    if self.working or not self.queue.empty():
                        return fallback("provider_busy")
                    future = asyncio.get_running_loop().create_future()
                    self.waiters[job["id"]] = future
                    job["active"] = True
                    job["deadline"] = deadline
                    self.queue.put_nowait(job)
                return self.result(await asyncio.shield(future))
        except TimeoutError:
            self.counts["active_timeout"] += 1
            return fallback("timeout")
        except Exception:
            self.counts["active_failed"] += 1
            return fallback("decision_unavailable")

    async def request(self, request):
        action, args = request.action, request.arguments
        if not isinstance(args, dict):
            raise ValueError("Decision arguments must be an object")
        if action == "status" and not args:
            return {**await self.receipts.call("status"), **self.diagnostics()}
        if action == "capabilities" and not args:
            return {
                **self.diagnostics(),
                "kinds": list(self.settings.thresholds),
                "guidance": "Use only for bounded semantic choices with evidence. Code handles exact rules. On fallback continue your original reasoning/workflow; never retry an action or claim authorization/completion from a judgment.",
            }
        if action == "reload" and not args:
            await self.reload()
            return self.diagnostics()
        if action == "list" and not set(args) - {"limit", "before"}:
            limit, before = args.get("limit", 20), args.get("before")
            if (
                type(limit) is not int
                or not 1 <= limit <= 100
                or (before is not None and (type(before) is not int or not 1 <= before < 2**63))
            ):
                raise ValueError("Invalid shadow page")
            return await self.receipts.call("list", limit, before)
        if action in {"get", "label"}:
            if set(args) != ({"id"} if action == "get" else {"id", "expected"}) or (
                not isinstance(args["id"], str) or not re.fullmatch(r"[0-9a-f]{64}", args["id"])
            ):
                raise ValueError("Invalid receipt request")
            if action == "label":
                if not isinstance(args["expected"], str):
                    raise ValueError("Label must be a choice")
                return await self.receipts.call("label", args["id"], args["expected"])
            return await self.receipts.call("get", args["id"])
        if action == "evaluate" and set(args) == {"kind", "reference", "state"}:
            return self.enqueue(DecisionSample(**args), "manual")
        raise ValueError("Unknown shadow action or arguments")

    def diagnostics(self):
        return {
            "enabled": self.settings.enabled,
            "external_data_consent": self.settings.external_data_consent,
            "mode": self.settings.mode,
            "active_enabled": self.settings.enabled
            and self.settings.external_data_consent
            and self.settings.mode == "active",
            "provider": "jev",
            "model_requested": self.settings.model,
            "credential_available": not self.auth_blocked,
            "error": self.error,
            "queue_depth": self.queue.qsize(),
            "queue_capacity": self.settings.queue_size,
            "active_timeout_seconds": self.settings.active_timeout_seconds,
            "transport": self.provider.diagnostics() if self.provider is not None else None,
            "thresholds": self.settings.thresholds,
            "retry_in_seconds": max(0, round(self.retry_at - time.monotonic())),
            "session_counts": dict(self.counts),
            "bus_dropped": self.context.runtime.dropped.get("decision-shadow", 0) if self.context else 0,
            "execution_enabled": False,
        }
