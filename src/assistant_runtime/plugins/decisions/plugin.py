"""Bounded optional suggestions with explicit fallbacks; never executes actions."""

from .declaration import DECLARATION

import asyncio
from collections import Counter
import re
import time
import uuid
import json
import hashlib
import logging
from dataclasses import asdict

from assistant_runtime.api.contracts import (
    DecisionRequest,
    DecisionSample,
    DecisionQuery,
    DecisionOutcome,
    TaskRequest,
    NotificationRequest,
)
from assistant_runtime.api.decisions import fallback
from .errors import DecisionError
from .questions import prepare, VERSIONS, validate_public_state
from .repository import Receipts
from .scheduling import DecisionQueue


class DecisionsPlugin:
    manifest = DECLARATION.manifest

    def __init__(self, api, provider=None):
        self.api = api
        self.settings = api.config
        self.provider = provider
        self.receipts = Receipts(api.state_dir, self.settings)
        self.queue = DecisionQueue(self.settings.queue_size)
        self.counts = Counter()
        self.error = None
        self.auth_blocked = True
        self.retry_at = 0
        self.failures = 0
        self.context = None
        self.provider_lock = asyncio.Lock()
        self.waiters = {}
        self.working = 0
        self.audit_error = None
        self.audit_queue = asyncio.Queue(maxsize=128)
        self.audit_dropped = 0
        self.outage = None
        self.calls = {}
        self.policy_version = hashlib.sha256(
            json.dumps(asdict(self.settings), sort_keys=True).encode()
        ).hexdigest()[:16]

    async def start(self, context):
        self.context = context
        context.own(self.receipts.close)
        context.own(self.drain_audit)
        context.spawn(self.audit_worker(), "archive")
        await self.receipts.call("open")
        if self.settings.enabled:
            try:
                if self.provider is None:
                    from .jev import JevProvider

                    self.provider = JevProvider(
                        None, self.settings, credential_source=lambda: self.api.credential("TYPESAFE_API_KEY")
                    )
                context.own(self.provider.close)
                await self.provider.start()
                await self.reload()
            except Exception:
                self.error = "provider_initialization_failed"
        self.events = context.subscribe(capacity=64)

        context.register(DecisionRequest, self.request)
        context.register(DecisionQuery, self.judge)
        context.register(DecisionOutcome, self.outcome)
        context.spawn(self.listen(), "samples")
        for index in range(self.settings.concurrency):
            context.spawn(self.worker(index == self.settings.concurrency - 1), "evaluate:" + str(index))
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
        await self.context.ready.wait()
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
                if self.settings.external_data_consent:
                    self.health_changed(str(exc))

    def enqueue(self, sample, source, usage_scope=None):
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
        from assistant_runtime.api.ai_usage import current_usage

        scope = usage_scope or current_usage.get()
        if scope is not None:
            job["usage_scope"] = asdict(scope)
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
                    self.enqueue(event.data.get("sample"), event.source, event.data.get("usage_scope"))
            finally:
                self.events.task_done()

    async def worker(self, interactive_only=False):
        while not self.context.cancelled.is_set():
            job = await self.queue.get(interactive_only)
            self.working += 1
            claimed = False
            started = time.monotonic()
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
                    remaining = (
                        job.get("deadline", time.monotonic() + self.settings.timeout_seconds)
                        - time.monotonic()
                    )
                    try:
                        async with asyncio.timeout(max(0.001, remaining)):
                            from assistant_runtime.api.ai_usage import UsageScope, UsageError
                            from assistant_runtime.api.ai_usage import metered

                            scope = (
                                UsageScope(**job["usage_scope"])
                                if job.get("usage_scope")
                                else await self.api.resource("usage").scope(independent="decision")
                            )
                            try:
                                result = await metered(
                                    self.api.resource("usage"),
                                    scope,
                                    "decision",
                                    "typesafe",
                                    self.settings.model,
                                    lambda: self.provider.evaluate(job),
                                    body=job.get("input"),
                                )
                            except UsageError as exc:
                                raise DecisionError(exc.code) from None
                    except TimeoutError:
                        raise DecisionError("timeout") from None
                except asyncio.CancelledError:
                    await self.receipts.call(
                        "finish", job["id"], "unknown", {"error": "shutdown_during_request"}
                    )
                    raise
                except DecisionError as exc:
                    self.health_changed(str(exc))
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
                    if not self.auth_blocked:
                        self.health_changed(None)
                        self.retry_at = 0
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
                row = None
                try:
                    row = await self.receipts.call("find", job["id"])
                    await self.audit(
                        {
                            "stage": "provider_result",
                            "receipt_id": job["id"],
                            "kind": job["kind"],
                            "source": job["source"],
                            "reference": job["reference"],
                            "result": row,
                            "queue_ms": round(max(0, started - job.get("queued_at", started)) * 1000),
                        }
                    )
                except Exception:
                    pass
                # A duplicate observation cannot settle the owning worker's waiters.
                if row is None or row["state"] != "dispatching":
                    future = self.waiters.pop(job["id"], None)
                    if future is not None and not future.done():
                        future.set_result(row)
                self.working -= 1
                self.queue.task_done()

    def result(self, row, policy=None):
        if row is None:
            return fallback("receipt_unavailable")
        if row["state"] != "completed":
            return fallback(row.get("error", row["state"]), row["id"])
        answer = row["answer"]
        choice = answer["choice"]
        threshold = (policy or self.settings.policy(row["kind"]))["threshold"]
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

    async def audit(self, entry):
        try:
            self.audit_queue.put_nowait({"at": time.time(), **entry})
        except asyncio.QueueFull:
            self.audit_dropped += 1
            self.audit_error = "archive_queue_full"
            logging.getLogger(__name__).warning("decision_archive_queue_full")

    async def write_audit(self, entry):
        try:
            await self.receipts.call("audit", entry)
            self.audit_error = None
        except Exception:
            self.audit_dropped += 1
            self.audit_error = "archive_unavailable"
            logging.getLogger(__name__).warning("decision_archive_unavailable")

    async def audit_worker(self):
        while True:
            entry = await self.audit_queue.get()
            try:
                await self.write_audit(entry)
            finally:
                self.audit_queue.task_done()

    async def drain_audit(self):
        while not self.audit_queue.empty():
            entry = self.audit_queue.get_nowait()
            try:
                await self.write_audit(entry)
            finally:
                self.audit_queue.task_done()

    def health_changed(self, error):
        if error and self.outage:
            return
        if not error and not self.outage:
            return
        previous = self.outage
        self.outage = {"id": uuid.uuid4().hex, "error": error} if error else None
        episode = self.outage or previous

        async def notify():
            try:
                await self.api.invoke(
                    NotificationRequest(
                        "publish",
                        {
                            "event_key": "decision-service:"
                            + episode["id"]
                            + (":failed" if error else ":recovered"),
                            "group_key": "decision-service",
                            "group_title": "快速决策服务",
                            "title": "Jev 暂不可用，业务沿用原逻辑" if error else "Jev 已恢复",
                            "body": "错误类别：" + error
                            if error
                            else "后续请求可以继续使用快速决策。之前的操作不会重放。",
                            "voice": False,
                        },
                    ),
                    timeout=2,
                )
            except Exception:
                logging.getLogger(__name__).warning("decision_health_notification_unavailable")

        if self.context:
            self.context.spawn(notify(), "decision-health")

    async def outcome(self, request):
        if (
            not re.fullmatch(r"[0-9a-f]{32}", request.decision_id)
            or request.status not in {"adopted", "rejected", "suggested", "completed", "failed", "unknown"}
            or not re.fullmatch(r"[a-z_]{1,80}", request.reason)
            or request.operation_id is not None
            and (not isinstance(request.operation_id, str) or len(request.operation_id) > 160)
        ):
            raise ValueError("Invalid decision outcome")
        await self.audit({**self.calls.get(request.decision_id, {}), "stage": "adoption", **asdict(request)})
        return {"queued": self.audit_error is None, "durability": "asynchronous_archive"}

    async def judge(self, request):
        ident = uuid.uuid4().hex
        if not isinstance(request.kind, str) or request.kind not in VERSIONS:
            await self.audit({"decision_id": ident, "stage": "result", "reason": "invalid_kind"})
            return {**fallback("invalid_kind"), "decision_id": ident}
        policy = self.settings.policy(request.kind)
        if not policy["enabled"] or not self.settings.external_data_consent:
            reason = "disabled" if not policy["enabled"] else "external_data_not_authorized"
            await self.audit(
                {"decision_id": ident, "kind": request.kind, "stage": "result", "reason": reason}
            )
            return {**fallback(reason), "decision_id": ident}
        meta = {
            "decision_id": ident,
            "kind": request.kind,
            "policy_version": self.policy_version,
            "policy": policy,
            "caller_run": request.caller_run,
            "input_version": request.input_version,
        }
        # Validate before logging any caller-supplied free text, including summaries.
        try:
            if (
                request.kind not in VERSIONS
                or not isinstance(request.summary, str)
                or len(request.summary) > 800
            ):
                raise ValueError("Invalid decision summary")
            if not isinstance(request.input_version, str) or len(request.input_version) > 160:
                raise ValueError("Invalid input version")
            validate_public_state(
                {
                    "summary": request.summary,
                    "reference": request.reference,
                    "caller_run": request.caller_run,
                    "input_version": request.input_version,
                }
            )
            job = prepare(
                DecisionSample(request.kind, request.reference, request.state),
                "validation",
                self.settings.model,
            )
        except (ValueError, TypeError, RecursionError):
            await self.audit({"decision_id": ident, "stage": "result", "reason": "invalid_request"})
            return {**fallback("invalid_request"), "decision_id": ident}
        meta.update(
            summary=request.summary,
            reference=request.reference,
            input_hash=job["input_hash"],
            candidates=job["choices"],
            question_version=job["question_version"],
        )
        self.calls[ident] = meta
        if len(self.calls) > self.settings.retention:
            del self.calls[next(iter(self.calls))]
        started = time.monotonic()
        await self.audit({**meta, "stage": "requested"})
        try:
            result = await self._judge(request, policy, started + policy["timeout_seconds"], meta)
        except asyncio.CancelledError:
            await self.audit({**meta, "stage": "result", "status": "cancelled", "reason": "caller_cancelled"})
            raise
        result = {
            **result,
            "decision_id": ident,
            "policy_version": self.policy_version,
            "mode": policy["mode"],
            "probability_threshold": policy["threshold"],
        }
        await self.audit(
            {**meta, "stage": "result", **result, "total_ms": round((time.monotonic() - started) * 1000)}
        )
        return result

    async def _judge(self, request, policy, deadline, meta):
        if not policy["enabled"]:
            return fallback("disabled")
        if not self.settings.external_data_consent:
            return fallback("external_data_not_authorized")
        if self.auth_blocked:
            return fallback("credential_unavailable")
        if time.monotonic() < self.retry_at:
            return fallback("provider_backoff")
        try:
            async with asyncio.timeout_at(deadline):
                if request.caller_run is not None:
                    info = await self.api.invoke(
                        TaskRequest("execution_info", {"run_ids": [request.caller_run]})
                    )
                    if info.get(request.caller_run, {}).get("state") not in {"running", "waiting_user"}:
                        return fallback("caller_not_active")
                source = {
                    "voice_intent": "assistant",
                    "event_relevance": "tasks",
                    "model_profile": "tasks",
                    "model_review": "tasks",
                }.get(request.kind, "mcp:background" if request.caller_run else "mcp:foreground")
                reference = (request.caller_run + ":" if request.caller_run else "") + request.reference
                job = prepare(
                    DecisionSample(request.kind, reference, request.state), source, self.settings.model
                )
                job["usage_scope"] = asdict(await self.api.resource("usage").scope(request.caller_run))
                meta["receipt_id"] = job["id"]
                await self.audit({**meta, "stage": "linked"})
                row = await self.receipts.call("find", job["id"])
                future = self.waiters.get(job["id"])
                if row and row["state"] != "dispatching" and future is None:
                    return (
                        fallback("shadow_mode", row["id"])
                        if policy["mode"] == "shadow"
                        else self.result(row, policy)
                    )
                if future is None:
                    if row is None and self.queue.full(interactive=request.kind == "voice_intent"):
                        return fallback("queue_full")
                    future = asyncio.get_running_loop().create_future()
                    self.waiters[job["id"]] = future
                    if row is None:
                        job.update(active=True, deadline=deadline, queued_at=time.monotonic(), policy=policy)
                        self.queue.put_nowait(job)
                if policy["mode"] == "shadow":
                    return fallback("shadow_mode", job["id"])
                return self.result(await asyncio.shield(future), policy)
        except TimeoutError:
            self.counts["active_timeout"] += 1
            return fallback("timeout", meta.get("receipt_id"))
        except Exception:
            self.counts["active_failed"] += 1
            return fallback("decision_unavailable", meta.get("receipt_id"))

    async def request(self, request):
        action, args = request.action, request.arguments
        if not isinstance(args, dict):
            raise ValueError("Decision arguments must be an object")
        if action == "status" and not args:
            return {
                **await self.receipts.call("status"),
                "audit": await self.receipts.call("audit_status"),
                **self.diagnostics(),
            }
        if action == "capabilities" and not args:
            return {
                **self.diagnostics(),
                "kinds": list(VERSIONS),
                "guidance": "Use only for bounded semantic choices with evidence. Code handles exact rules. On fallback continue your original reasoning/workflow; never retry an action or claim authorization/completion from a judgment.",
            }
        if action == "reload" and not args:
            await self.reload()
            return self.diagnostics()
        if action == "archive":
            return await self.receipts.call("archive", args)
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
            "thresholds": {k: self.settings.policy(k)["threshold"] for k in VERSIONS},
            "policies": {k: self.settings.policy(k) for k in VERSIONS},
            "policy_version": self.policy_version,
            "archive_error": self.audit_error,
            "archive_dropped": self.audit_dropped,
            "concurrency": self.settings.concurrency,
            "in_flight": self.working,
            "retry_in_seconds": max(0, round(self.retry_at - time.monotonic())),
            "session_counts": dict(self.counts),
            "bus_dropped": self.context.dropped if self.context else 0,
            "execution_enabled": False,
        }
