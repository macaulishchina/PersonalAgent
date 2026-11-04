"""Task-owned dispatch evidence, resource waits and explicit recovery. Never replay work."""

import time
import traceback
from pathlib import Path

from assistant_runtime.api.tool_calls import ToolFailure, ToolOutcome


def locks(run):
    # A pre-existing snapshot without the new field keeps its original quarantine.
    # New specs explicitly capture an independent lock list, never inferred grants.
    spec = run["data"]["spec"]
    return spec.get("resource_locks", spec.get("resources", []))


def dispatched(run):
    evidence = run["data"].get("dispatch")
    if evidence:
        return evidence.get("state", "unknown")
    return "unknown"  # Missing historical IDs are not a non-dispatch receipt.


class TaskLifecycle:
    def __init__(self, plugin):
        self.plugin, self.repo = plugin, plugin.repo

    def mark(self, ident, stage, state):
        run = self.repo.run(ident)
        marker = {
            "stage": stage,
            "state": state,
            "at": time.time(),
            "round": run["data"].get("completion_round", 0),
        }
        self.repo.record(ident, "dispatch", marker)
        self.repo.update_run(ident, dispatch=marker)

    def failure(self, ident, exc, *, kind="launch_error"):
        run = self.repo.run(ident)
        state = dispatched(run)
        if isinstance(exc, ToolFailure) and exc.outcome.dispatch == "not_dispatched":
            state = "not_dispatched"
        error = {
            "type": type(exc).__name__,
            "code": getattr(exc, "code", getattr(getattr(exc, "outcome", None), "code", kind)),
            "stage": run["data"].get("dispatch", {}).get("stage", "unrecorded"),
            "dispatch": state,
        }
        if isinstance(exc, ToolFailure):
            error["message"] = exc.outcome.message[:1000]
        error["frames"] = [
            {"file": Path(frame.filename).name, "line": frame.lineno, "function": frame.name}
            for frame in traceback.extract_tb(exc.__traceback__)[-8:]
        ]
        self.repo.record(ident, kind, error)
        first = run["data"].get("initial_error") or error
        self.repo.update_run(ident, initial_error=first, last_error=error)
        return "failed" if state == "not_dispatched" else "unknown"

    def wait(self, run, busy, pending):
        conflicts = []
        wanted = set(locks(run))
        for other in pending:
            if other["id"] == run["id"]:
                continue
            if other in busy or other["state"] == "unknown":
                # Old unknown scope was a grant/lock hybrid: preserve its quarantine
                # even when the new caller does not request a scheduling mutex.
                requested = wanted | (
                    set(run["data"]["spec"].get("resources", []))
                    if "resource_locks" not in other["data"]["spec"]
                    else set()
                )
                overlap = sorted(requested.intersection(locks(other)))
                if overlap:
                    conflicts.append(
                        {
                            "run_id": other["id"],
                            "task_id": other["task_id"],
                            "state": other["state"],
                            "resources": overlap,
                        }
                    )
        reason = (
            {"kind": "resource", "blockers": conflicts, "next_action": "reconcile_blocking_run"}
            if conflicts
            else {"kind": "capacity", "next_action": "wait"}
            if len(busy) >= self.plugin.settings.concurrency
            else None
        )
        if run["data"].get("waiting") != reason:
            self.repo.record(run["id"], "waiting", {"reason": reason})
            self.repo.update_run(run["id"], waiting=reason)
        return reason

    async def reconcile(self, args):
        run = self.repo.run(args["run_id"])
        if run["id"] in self.plugin.launching:
            raise ValueError("Execution is still preparing; wait for its dispatch boundary")
        if run["state"] not in {"unknown", "paused", "failed", "canceled"}:
            raise ValueError("Reconciliation requires a stopped or unknown execution")
        execution = getattr(self.plugin.host.backend, "executions", None)
        canonical = execution.task_evidence(run["id"]) if execution else []
        if (
            not run["data"].get("execution_session_id")
            and len(canonical) == 1
            and canonical[0].get("session_id")
        ):
            matched = canonical[0]
            self.repo.record(
                run["id"],
                "identity_reconciled",
                {"execution_id": matched["id"], "source": "execution_ledger"},
            )
            run = self.repo.update_run(
                run["id"],
                execution_id=matched["id"],
                execution_session_id=matched["session_id"],
                turn_id=matched.get("turn_id"),
            )
        result = {
            "dispatch": dispatched(run),
            "attempts": canonical,
            "external_effects": "unknown",
            "safe_to_retry": False,
            "state": "unknown",
        }
        inspect = getattr(self.plugin.executor, "recovery_evidence", None)
        if inspect:
            try:
                result["local"] = await inspect(run)
            except Exception as exc:
                result["local"] = {"state": "unknown", "error": type(exc).__name__}
        settled = all(
            a["state"] in {"completed", "failed", "cancelled", "interrupted"}
            and not a.get("unresolved_operations")
            for a in canonical
        )
        if result["dispatch"] == "not_dispatched" and settled:
            result.update(state="failed", external_effects="not_dispatched")
        else:
            try:
                state, text = await self.plugin.executor.reconcile(run)
                result["provider_state"] = state
                unresolved = any(
                    a.get("unresolved_operations")
                    or a["state"]
                    in {"queued", "dispatching", "running", "waiting_user", "cancelling", "unknown"}
                    for a in canonical
                )
                local = result.get("local", {})
                unresolved = unresolved or local.get("active") or local.get("state") == "unknown"
                if state in {"completed", "failed", "canceled"} and not unresolved:
                    result.update(state=state, external_effects="provider_reported")
            except Exception as exc:
                result["error"] = type(exc).__name__
        self.repo.record(run["id"], "reconciliation", result)
        if run["state"] == "unknown" and result["state"] != "unknown":
            self.repo.update_run(
                run["id"],
                result["state"],
                turn_active=False,
                result=text if result["state"] == "completed" else run["data"].get("result", ""),
                stop_requested=None,
            )
        self.repo.update_run(run["id"], recovery=result)
        return self.repo.run(run["id"])

    async def resolve(self, args):
        run = self.repo.run(args["run_id"])
        reason = args.get("reason")
        if (
            run["state"] != "unknown"
            or args.get("revision") != run["revision"]
            or args.get("confirmed") is not True
            or not isinstance(reason, str)
            or not 10 <= len(reason.strip()) <= 2000
            or not run["data"].get("recovery")
            or run["id"] in self.plugin.launching
        ):
            raise ValueError(
                "Reconcile first, then confirm the latest revision with verified external-operation evidence (10–2000 characters)"
            )
        # Stop/inspect owned local work before accepting an operator's external attestation.
        # Historical missing sessions remain uncertain, and can only be closed explicitly.
        inspect = getattr(self.plugin.executor, "recovery_evidence", None)
        if run["data"].get("execution_session_id"):
            local = await inspect(run) if inspect else {"active": True}
            if local.get("active"):
                await self.plugin.executor.stop(run)
        execution = getattr(self.plugin.host.backend, "executions", None)
        for attempt in execution.task_evidence(run["id"], all_attempts=True) if execution else []:
            if attempt["state"] in {
                "queued",
                "dispatching",
                "running",
                "waiting_user",
                "cancelling",
                "unknown",
            }:
                session = attempt.get("session_id")
                if not session:
                    raise ValueError(
                        "Canonical attempt has no local identity; inspect its operation receipts before closing"
                    )
                if session != run["data"].get("execution_session_id"):
                    bound = {
                        **run,
                        "data": {
                            **run["data"],
                            "execution_session_id": session,
                            "turn_id": attempt.get("turn_id"),
                        },
                    }
                    local = await inspect(bound, recorded=True) if inspect else {"active": True}
                    if local.get("active"):
                        await self.plugin.executor.stop(bound, recorded=True)
                        local = await inspect(bound, recorded=True) if inspect else {"active": False}
                    if local.get("active") or local.get("state") == "unknown":
                        raise ValueError("Canonical local process has not stopped; retain quarantine")
        if inspect:
            local = await inspect(run)
            if local.get("active") or (
                local.get("state") == "unknown" and run["data"].get("execution_session_id")
            ):
                raise ValueError("Local execution/commands are not confirmed stopped; retain quarantine")
        current = self.repo.run(run["id"])
        if current["revision"] != run["revision"] or current["state"] != "unknown":
            raise ValueError("Execution changed during verification; reload before resolving")
        self.repo.record(
            run["id"],
            "operator_resolution",
            {
                "authority": "operator",
                "reason": reason.strip(),
                "revision": run["revision"],
                "outcome": "canceled",
                "business_success": "not_asserted",
                "resources": locks(run),
            },
        )
        self.repo.update_run(
            run["id"],
            "canceled",
            turn_active=False,
            stop_requested=None,
            progress="操作者核对后已收尾；未重做，也不代表业务成功。",
        )
        return self.repo.run(run["id"])

    def conflict(self, runs):
        raise ToolFailure(
            ToolOutcome(
                "task_run_conflict",
                "validation",
                "not_dispatched",
                "failed",
                "任务已有未结束的执行，请查看对应执行；核对或收尾后再运行。",
                next_action="inspect_existing_run",
                receipt={
                    "task_id": runs[0]["task_id"],
                    "run_id": runs[0]["id"],
                    "runs": [
                        {
                            "task_id": r["task_id"],
                            "run_id": r["id"],
                            "state": r["state"],
                            "waiting": r["data"].get("waiting"),
                        }
                        for r in runs
                    ],
                },
            )
        )
