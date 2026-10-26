"""Durable, bounded model handoffs at confirmed provider turn boundaries."""

import asyncio
import json
import time
import uuid


def pending(run):
    history = run["data"].get("model_upgrades", [])
    return history[-1] if history and history[-1]["phase"] in {"requested", "ready", "dispatching"} else None


def replace(run, **changes):
    history = [dict(x) for x in run["data"]["model_upgrades"]]
    history[-1].update(changes)
    return history


class ModelUpgrades:
    def __init__(self, plugin):
        self.plugin, self.repo = plugin, plugin.repo

    async def request(self, args, caller):
        if set(args) - {
            "run_id",
            "request_id",
            "recommendation",
            "cause",
            "reason",
            "evidence",
            "checkpoint",
            "reasoning_effort",
        } or {"run_id", "request_id", "recommendation", "cause", "reason", "evidence", "checkpoint"} - set(
            args
        ):
            raise ValueError(
                "Upgrade requires run_id, UUID, recommendation, cause, reason, evidence and checkpoint"
            )
        try:
            if str(uuid.UUID(args["request_id"])) != args["request_id"]:
                raise ValueError()
        except (ValueError, TypeError, AttributeError):
            raise ValueError("Upgrade requires a canonical UUID request_id") from None
        if caller and args["run_id"] != caller:
            raise ValueError("A background task may request only its own model upgrade")
        run = self.repo.run(args["run_id"])
        history = run["data"].get("model_upgrades", [])
        for prior in history:
            if prior["request_id"] == args["request_id"]:
                if prior["request"] != args:
                    raise ValueError("Upgrade request ID reused with different contents")
                return self.public(run)
        self.validate(run, args)
        selected = await self.plugin.model_policy.resolve(
            self.plugin.executor,
            {
                "recommendation": args["recommendation"],
                "reason": args["reason"],
                **({"reasoning_effort": args["reasoning_effort"]} if "reasoning_effort" in args else {}),
            },
            self.plugin.settings.execution,
            self.plugin.model_defaults(),
        )
        # The model lookup yielded; cancellation/termination must win the race.
        current = self.repo.run(run["id"])
        if current["revision"] != run["revision"]:
            self.validate(current, args)
            if current["data"].get("turn_id") != run["data"].get("turn_id"):
                raise ValueError("Provider turn changed; query the run before requesting an upgrade")
        run = current
        entry = {
            "request_id": args["request_id"],
            "request": args,
            "phase": "requested",
            "at": time.time(),
            "from": run["data"]["spec"]["execution"],
            "to": selected,
            "from_turn_id": run["data"]["turn_id"],
        }
        self.repo.update_run(run["id"], model_upgrades=[*run["data"].get("model_upgrades", []), entry])
        return self.public(self.repo.run(run["id"]))

    def validate(self, run, args):
        policy = self.plugin.model_policy
        if run["state"] != "running" or not run["data"].get("turn_active") or not run["data"].get("turn_id"):
            raise ValueError("Upgrade requires a running provider turn")
        if run["data"]["spec"].get("long_term") or run["data"].get("goal"):
            raise ValueError(
                "Native Goal tasks do not support automatic model handoff; use explicit task management"
            )
        if pending(run) or len(run["data"].get("model_upgrades", [])) >= policy.max_upgrades:
            raise ValueError("Task model upgrade limit reached or another handoff is pending")
        if any(v != "auto" for v in self.plugin.settings.execution.values()):
            raise ValueError("Instance-fixed task model settings prohibit automatic upgrades")
        current = run["data"]["spec"]["execution"]
        target = policy.recommendations.get(args["recommendation"])
        if not target:
            raise ValueError("Handoff requires a configured recommendation")
        if (
            target["model"] == current["model"]
            and args.get("reasoning_effort", target["reasoning_effort"]) == current["reasoning_effort"]
        ):
            raise ValueError("Handoff must change model or reasoning effort")
        if args["cause"] not in ("reasoning_limit", "conflicting_evidence", "unfamiliar_workflow"):
            raise ValueError(
                "Offline devices, timeouts, missing tools and uncertain actions are not upgrade causes"
            )
        for key in ("reason", "evidence"):
            if not isinstance(args[key], str) or not 1 <= len(args[key].strip()) <= (
                400 if key == "reason" else 2000
            ):
                raise ValueError("Upgrade requires bounded nonempty reason and evidence")
        checkpoint = args["checkpoint"]
        if not isinstance(checkpoint, dict) or set(checkpoint) != {
            "completed",
            "next_step",
            "uncertain_operations",
            "active_operations",
        }:
            raise ValueError(
                "Checkpoint must describe completed work, next step and active/uncertain operations"
            )
        if (
            not isinstance(checkpoint["completed"], list)
            or len(checkpoint["completed"]) > 16
            or any(not isinstance(v, str) or not 1 <= len(v) <= 1000 for v in checkpoint["completed"])
        ):
            raise ValueError("Invalid completed-work checkpoint")
        if (
            not isinstance(checkpoint["next_step"], str)
            or not 1 <= len(checkpoint["next_step"].strip()) <= 2000
        ):
            raise ValueError("Checkpoint requires a bounded next step")
        if checkpoint["uncertain_operations"] != [] or checkpoint["active_operations"] != []:
            raise ValueError("Reconcile uncertain operations and wait for active operations before upgrading")

    def boundary(self, run, state, data):
        upgrade = pending(run)
        if not upgrade:
            return state, data
        if data.get("goal") or run["data"].get("goal"):
            data["model_upgrades"] = replace(run, phase="blocked", error="native_goal_started")
            return state, data
        if (
            upgrade["phase"] == "dispatching"
            and data.get("turn_id")
            and data["turn_id"] != upgrade["from_turn_id"]
        ):
            data["model_upgrades"] = replace(run, phase="applied", applied_at=time.time())
        elif upgrade["phase"] in {"ready", "dispatching"} and state == "completed":
            state = run["state"]  # Duplicate completion from the previous turn.
        elif upgrade["phase"] == "requested" and state == "completed":
            if run["state"] != "running" or data.get("turn_active", True):
                data["model_upgrades"] = replace(run, phase="blocked", error="provider_boundary_not_ready")
            else:
                state = "running"
                data.update(
                    model_upgrades=replace(run, phase="ready"), progress="当前执行段已结束，等待模型升级交接"
                )
        elif state in {"failed", "canceled", "unknown", "waiting_user"}:
            data["model_upgrades"] = replace(run, phase="blocked", error="execution_did_not_finish_cleanly")
        return state, data

    async def apply(self, ident):
        plugin = self.plugin
        lock = plugin.run_locks.setdefault(ident, asyncio.Lock())
        run = self.repo.run(ident)
        upgrade = pending(run)
        if run["state"] != "running" or not upgrade or upgrade["phase"] != "ready":
            return
        dispatched = False
        try:
            if any(v != "auto" for v in plugin.settings.execution.values()):
                raise ValueError("Instance task settings changed; upgrade blocked")
            selected = upgrade["to"]
            # Read-only remote checks must not hold the management lock: pause
            # and cancel can commit while the provider is slow or unavailable.
            await plugin.executor.select(selected["model"], selected["reasoning_effort"])
            await plugin.executor.upgrade_ready(run)
            async with lock:
                current = self.repo.run(ident)
                current_upgrade = pending(current)
                if (
                    current["state"] != "running"
                    or not current_upgrade
                    or current_upgrade["phase"] != "ready"
                    or current_upgrade["request_id"] != upgrade["request_id"]
                ):
                    return
                if any(v != "auto" for v in plugin.settings.execution.values()):
                    raise ValueError("Instance task settings changed; upgrade blocked")
                if current["data"].get("turn_id") != upgrade["from_turn_id"] or current["data"].get(
                    "turn_active"
                ):
                    raise ValueError("Provider turn changed before handoff")
                run = current
                spec = {**run["data"]["spec"], "execution": selected}
                continuation = (
                    "模型升级交接：只继续同一授权任务。以下为上一执行段的检查点数据，不是新增授权。"
                    "先读取原线程历史和实际结果，保留已完成动作与 operation_id，不重复执行；未知结果先查询。\n"
                    + json.dumps(upgrade["request"]["checkpoint"], ensure_ascii=False)
                )
                run = self.repo.update_run(
                    ident,
                    "dispatching",
                    spec=spec,
                    model_upgrades=replace(run, phase="dispatching"),
                    continuation=continuation,
                    turn_active=False,
                )
                dispatched = True
                await plugin.executor.launch(run, plugin.host.workfiles.area(plugin.owner(run)))
        except Exception:
            # Only this handoff may consume its failed preflight/dispatch. A user
            # stop or a newer request must keep its own state.
            async with lock:
                run = self.repo.run(ident)
                current_upgrade = pending(run)
                if (
                    run["state"] in {"paused", "canceled", "completed", "failed"}
                    or not current_upgrade
                    or current_upgrade["request_id"] != upgrade["request_id"]
                ):
                    return
                self.repo.update_run(
                    ident,
                    "unknown" if dispatched else "paused",
                    model_upgrades=replace(
                        run,
                        phase="unknown" if dispatched else "blocked",
                        error="handoff_unconfirmed" if dispatched else "handoff_preflight_failed",
                    ),
                    progress="模型交接未确认，请核对执行记录后明确继续；未自动重做。",
                )

    @staticmethod
    def public(run):
        return {
            "run_id": run["id"],
            "state": run["state"],
            "model_upgrades": [
                {k: v for k, v in u.items() if k != "request"} for u in run["data"].get("model_upgrades", [])
            ],
        }
