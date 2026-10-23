"""Explicit ordered fallback for rejected first turns; never replay completed work."""

import asyncio
import logging
import time

log = logging.getLogger(__name__)


class ModelFallbacks:
    def __init__(self, plugin):
        self.plugin = plugin

    def boundary(self, run, state, data):
        failure = data.get("model_failure")
        previous = run["data"].get("model_fallback", {})
        if state == "completed" and previous.get("phase") == "applied":
            data["model_fallback"] = {**previous, "phase": "completed"}
        if state != "failed" or not isinstance(failure, dict):
            return state, data
        current = run["data"]["spec"]["execution"]
        history = run["data"].get("model_fallback", {})
        candidates = history.get("candidates", self.plugin.model_policy.fallbacks.get(current["model"], []))
        index = history.get("next_index", 0)
        allowed = bool(failure.get("fallback_allowed") and candidates and index < len(candidates))
        log.error(
            "Task model failed run=%s model=%s error=%s fallback_configured=%s",
            run["id"],
            current["model"],
            failure.get("code"),
            allowed,
        )
        if (
            not allowed
            or run["data"]["spec"].get("long_term")
            or run["data"].get("goal")
            or run["data"].get("model_upgrades")
        ):
            if history:
                data["model_fallback"] = {
                    **history,
                    "phase": "exhausted" if index >= len(candidates) else "blocked",
                    "attempts": [
                        *history.get("attempts", []),
                        {"model": current["model"], "error": failure["code"], "at": time.time()},
                    ],
                }
            return state, data
        data["model_fallback"] = {
            "phase": "ready",
            "candidates": candidates,
            "next_index": index,
            "from_thread": run["data"].get("execution_session_id"),
            "from_turn": data.get("turn_id", run["data"].get("turn_id")),
            "attempts": [
                *history.get("attempts", []),
                {"model": current["model"], "error": failure["code"], "at": time.time()},
            ],
        }
        data["progress"] = "模型调用失败，核验尚未执行操作后按配置尝试备选模型。"
        return "running", data

    async def apply(self, ident):
        plugin = self.plugin
        run = plugin.repo.run(ident)
        pending = run["data"].get("model_fallback", {})
        if run["state"] != "running" or pending.get("phase") != "ready":
            return
        lock = plugin.run_locks.setdefault(ident, asyncio.Lock())
        dispatched = False
        try:
            target = pending["candidates"][pending["next_index"]]
            selected = await plugin.executor.select(target["model"], target["reasoning_effort"])
            await plugin.executor.fallback_ready(run)
            await plugin.executor.finalize({**run, "state": "failed"})
            await plugin.executor.detach(run)
            async with lock:
                latest = plugin.repo.run(ident)
                if latest["state"] != "running" or latest["data"].get("model_fallback") != pending:
                    return
                if latest["data"].get("turn_id") != pending["from_turn"] or latest["data"].get("turn_active"):
                    raise RuntimeError("Execution changed before fallback")
                execution = {**latest["data"]["spec"]["execution"], **selected}
                execution["reason"] = "按实例配置切换备选；原模型明确失败且尚未执行操作"
                execution["selection"] = {
                    "version": 1,
                    "at": time.time(),
                    "profile": execution.get("profile", "custom"),
                    "sources": {"model": "configured_fallback", "reasoning_effort": "configured_fallback"},
                    "reason": execution["reason"],
                    "resolved": selected,
                }
                latest = plugin.repo.update_run(
                    ident,
                    "dispatching",
                    spec={**latest["data"]["spec"], "execution": execution},
                    model_fallback={
                        **pending,
                        "phase": "dispatching",
                        "next_index": pending["next_index"] + 1,
                        "selected": selected,
                    },
                    previous_execution_session_id=pending["from_thread"],
                    execution_session_id=None,
                    turn_id=None,
                    turn_active=False,
                    provider_error=None,
                    model_failure=None,
                )
                dispatched = True
            log.warning(
                "Task model fallback run=%s from=%s to=%s",
                ident,
                run["data"]["spec"]["execution"]["model"],
                selected["model"],
            )
            # Dispatch outside the management lock; pause/cancel can revoke authority.
            if plugin.repo.run(ident)["state"] != "dispatching":
                return
            await plugin.executor.launch(latest, plugin.host.workfiles.area(plugin.owner(latest)))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("Task model fallback blocked run=%s reason=%s", ident, type(exc).__name__)
            async with lock:
                current = plugin.repo.run(ident)
                active = current["data"].get("model_fallback", {})
                if (
                    current["state"] in {"running", "dispatching"}
                    and active.get("from_turn") == pending["from_turn"]
                    and active.get("phase") in {"ready", "dispatching"}
                ):
                    plugin.repo.update_run(
                        ident,
                        "unknown" if dispatched else "failed",
                        model_fallback={
                            **active,
                            "phase": "unknown" if dispatched else "blocked",
                            "error": type(exc).__name__,
                        },
                        progress="备选模型切换未完成；保留执行记录，未自动重做。",
                    )
