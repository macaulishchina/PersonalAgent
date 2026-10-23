"""Bounded, observational model review. Never changes execution or grants authority."""

import asyncio
import hashlib
import json
import time

from ...kernel.contracts import DecisionQuery
from ...kernel.decisions import decide, fallback


def assessment(value):
    if value is None:
        return {}
    if not isinstance(value, dict) or set(value) - {
        "acceptance",
        "procedure",
        "uncertainties",
        "user_preference",
        "prior_run_id",
    }:
        raise ValueError("Invalid execution assessment")
    for key, item in value.items():
        if key == "uncertainties":
            if (
                not isinstance(item, list)
                or len(item) > 4
                or any(not isinstance(s, str) or not 1 <= len(s.strip()) <= 300 for s in item)
            ):
                raise ValueError("Assessment uncertainties require up to four bounded statements")
        elif not isinstance(item, str) or not 1 <= len(item.strip()) <= (
            64 if key == "prior_run_id" else 500
        ):
            raise ValueError("Invalid assessment " + key)
    return json.loads(json.dumps(value))


def evidence(plugin, declared, caller, workspace):
    """Read related execution evidence ourselves; do not accept caller-supplied outcomes."""
    ident = declared.get("prior_run_id")
    if not ident:
        return None
    run = plugin.repo.run(ident)
    if caller:
        parent = plugin.repo.run(caller)
        if run["task_id"] != parent["task_id"] and run["data"]["spec"].get("parent_id") != parent["task_id"]:
            raise ValueError("Prior execution is outside the parent task scope")
    if run["data"]["spec"]["workspace"] != workspace:
        raise ValueError("Prior execution belongs to a different workspace")
    return {
        "run_id": ident,
        "state": run["state"],
        "title": run["data"]["spec"]["title"],
        "model": run["data"]["spec"]["execution"].get("model"),
        "reasoning_effort": run["data"]["spec"]["execution"].get("reasoning_effort"),
        "reported_summary": (run["data"].get("progress") or "")[:1200],
        "summary_truncated": len(run["data"].get("progress") or "") > 1200,
        "meaning": "Execution state is not goal success; reported summary is not independent verification.",
    }


class ModelReview:
    def __init__(self, plugin):
        self.plugin = plugin
        self.queue = asyncio.Queue(maxsize=16)

    def recover(self):
        # Do not replay paid judgments or executions after a restart.
        for ident, raw in self.plugin.repo.db.execute(
            "SELECT id,data FROM job_runs WHERE json_extract(data,'$.model_review.state') IN ('queued','running')"
        ).fetchall():
            review = json.loads(raw)["model_review"]
            self.save(ident, {**review, "state": "interrupted", "reason": "service_restart"})

    def save(self, ident, value):
        self.plugin.repo.update_run(ident, model_review=value)

    def enqueue(self, run):
        p = self.plugin
        if run["data"].get("model_review"):
            return
        if p.model_policy.review_mode == "active":
            receipt = run["data"]["spec"]["execution"].get("selection", {}).get("model_review")
            self.save(
                run["id"],
                {
                    **(receipt or {}),
                    "mode": "active",
                    "state": "selected" if receipt else "skipped",
                    "reason": None if receipt else "existing_selection_preserved",
                },
            )
            return
        config = p.host.config.decisions
        reason = (
            "disabled"
            if p.model_policy.review_mode == "off"
            else "decision_support_unavailable"
            if not (
                config.get("enabled", True)
                and config.get("external_data_consent", False)
                and config.get("mode", "active") == "active"
            )
            else "instance_fixed"
            if all(v != "auto" for v in p.settings.execution.values())
            else "queue_full"
            if self.queue.full()
            else None
        )
        execution = run["data"]["spec"]["execution"]
        record = {
            "version": 1,
            "baseline_scope": "initial_dispatch",
            "mode": "shadow",
            "state": "skipped" if reason else "queued",
            "reason": reason,
            "at": time.time(),
            "actual": {k: execution[k] for k in ("model", "reasoning_effort")},
            "selection_sources": execution.get("selection", {}).get("sources", {}),
            "recommendation_origin": "assistant",
            "user_preference_verified": False,
        }
        self.save(run["id"], record)
        if not reason:
            self.queue.put_nowait(run["id"])

    async def evaluate(self, ident):
        from ...kernel.ai_usage import current_usage

        usage = getattr(self.plugin.host, "ai_usage", None)
        token = current_usage.set(await usage.scope(ident)) if usage else None
        try:
            return await self._evaluate(ident)
        finally:
            if token is not None:
                current_usage.reset(token)

    async def _evaluate(self, ident):
        p = self.plugin
        run = p.repo.run(ident)
        record = run["data"]["model_review"]
        self.save(ident, {**record, "state": "running"})
        spec = run["data"]["spec"]
        execution = spec["execution"]
        catalog = await p.executor.models()
        options = p.model_policy.options(catalog, {}, p.settings.execution)
        # Explicit custom models remain candidates even when not assigned to a tier.
        if not any(o["model"] == execution["model"] for o in options.values()):
            options.update(
                p.model_policy.options(
                    catalog,
                    {"model": execution["model"], "reason": "review candidate"},
                    p.settings.execution,
                )
            )
        state = {
            "objective": spec["title"] + "\n" + spec["prompt"],
            "assessment": execution.get("assessment", {}),
            "prior_execution": execution.get("selection", {}).get("prior_execution"),
            "options": options,
        }
        if len(options) <= 1:
            self.save(ident, {**record, "state": "skipped", "reason": "no_selection_space"})
            return
        # The original recommendation/reason is deliberately excluded from the judgment.
        digest = hashlib.sha256(json.dumps(state, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        result = await decide(p.host.runtime, DecisionQuery("model_review", "task-review:" + digest, state))
        choice = options.get(result.get("choice")) if result["status"] == "ok" else None
        if result["status"] == "ok" and not choice:
            result = fallback("invalid_review_candidate", result.get("receipt_id"))
        recommended = {k: choice[k] for k in ("model", "reasoning_effort")} if choice else None
        self.save(
            ident,
            {
                **record,
                "state": "completed" if recommended else "fallback",
                "decision": result,
                "input_hash": digest,
                "recommended": recommended,
                "disagrees": recommended != record["actual"] if recommended else None,
                "finished_at": time.time(),
                "applied": False,
            },
        )

    async def worker(self):
        while True:
            ident = await self.queue.get()
            try:
                async with asyncio.timeout(4):
                    await self.evaluate(ident)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                old = self.plugin.repo.run(ident)["data"]["model_review"]
                self.save(ident, {**old, "state": "fallback", "reason": type(exc).__name__, "applied": False})
            finally:
                self.queue.task_done()
