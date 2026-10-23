"""Scoped native-voice suggestions and explicit execution-model confirmation.

This owner does not intercept native automatic handoffs or undo issued device actions.
"""

import asyncio
import time

from assistant_runtime.api.contracts import TaskRequest
from assistant_runtime.api.decisions import report_decision


class VoiceControl:
    def __init__(self, host):
        self.host = host
        self.links = {}
        self.focus_turn = None
        self.link_epochs = {}
        self.pending = None
        self.lock = asyncio.Lock()

    def bind(self, turn, task_id):
        if not turn or not task_id:
            return
        epoch = self.host.voice.epoch
        links = self.links.setdefault(turn, {})
        if task_id in links:
            return
        links[task_id] = None  # An unknown original run must never target a later schedule.
        self.focus_turn = turn
        self.link_epochs[turn] = epoch
        if len(self.links) > 32:
            old = next(iter(self.links))
            del self.links[old]
            self.link_epochs.pop(old, None)
        return self.host.context.spawn(self.capture_binding(task_id, links, epoch), "voice-task-binding")

    async def capture_binding(self, task_id, links, epoch):
        try:
            page = await self.host.runtime.invoke(TaskRequest("get", {"task_id": task_id}), timeout=2)
            runs = page.get("active_runs", [])
            if len(runs) == 1 and self.host.voice.epoch == epoch:
                links[task_id] = runs[0]["id"]
        except Exception:
            pass

    def capture_turn(self):
        turn = self.host.backend.active_turn
        receipt = self.host.conversation.feedback.get(turn) if turn else None
        if turn:
            return (
                turn
                if receipt and receipt["source"] == "voice" and receipt["epoch"] == self.host.voice.epoch
                else None
            )
        return self.focus_turn if self.link_epochs.get(self.focus_turn) == self.host.voice.epoch else None

    def current(self, proposal):
        h = self.host
        return (
            self.pending is proposal
            and h.active
            and not h.quiet_active()
            and h.voice.epoch == proposal["epoch"]
            and h.input_count == proposal["input_revision"]
            and h.conversation.voice_revision == proposal["focus"]
            and time.monotonic() < proposal["expires"]
        )

    async def offer(self, result, text, epoch, revision, turn, focus):
        proposal = {
            "decision": result,
            "decision_id": result.get("decision_id"),
            "text": text,
            "choice": result["choice"],
            "epoch": epoch,
            "input_revision": revision,
            "turn": turn,
            "focus": focus,
            "expires": time.monotonic() + 30,
            "task_ids": tuple(self.links.get(turn, ())),
            "runs": dict(self.links.get(turn, {})),
            "state": "delivering",
        }
        if self.pending and self.pending["state"] in {"pending", "delivering"}:
            await report_decision(
                self.host.runtime, self.pending["decision"], "rejected", "superseded_suggestion"
            )
        self.pending = proposal
        # Context-only injection; never submit the transcript as another user turn.
        try:
            await self.host.backend.import_voice_messages(
                [
                    {
                        "role": "assistant",
                        "text": (
                            "框架语音判断建议（不是已执行动作）："
                            + result["choice"]
                            + "。请通过 decisions 的 get_voice_decision 查看当前建议；理解用户原话后，"
                            "用 confirm_voice_decision 明确接受或拒绝。确认前不要另行重复下发纠正操作。"
                        ),
                    }
                ]
            )
        except asyncio.CancelledError:
            proposal["state"] = "expired"
            if self.pending is proposal:
                self.pending = None
            raise
        except Exception:
            proposal["state"] = "unavailable"
            if self.pending is proposal:
                self.pending = None
            await report_decision(self.host.runtime, result, "rejected", "suggestion_delivery_failed", turn)
            return
        if not self.current(proposal):
            proposal["state"] = "expired"
            await report_decision(self.host.runtime, result, "rejected", "stale_input", turn)
            return
        proposal["state"] = "pending"
        await report_decision(self.host.runtime, result, "suggested", "awaiting_execution_confirmation", turn)

    async def request(self, request):
        if request.caller_run is not None:
            raise ValueError("Voice control belongs to the foreground session")
        p = self.pending
        if not p or not self.current(p):
            if p and p["state"] == "pending":
                p["state"] = "expired"
                await report_decision(self.host.runtime, p["decision"], "rejected", "stale_input", p["turn"])
            return {"state": "unavailable"}
        if request.action == "get":
            return {k: p[k] for k in ("decision_id", "text", "choice", "state", "task_ids")}
        if request.action != "confirm" or set(request.arguments) != {"decision_id", "accept"}:
            raise ValueError("Invalid voice confirmation")
        if (
            request.arguments["decision_id"] != p["decision_id"]
            or type(request.arguments["accept"]) is not bool
        ):
            raise ValueError("Stale or invalid voice confirmation")
        async with self.lock:
            if self.pending is not p or not self.current(p):
                return {"state": "expired"}
            if p["state"] != "pending":
                return {"state": p["state"], "replayed": False}
            if not request.arguments["accept"]:
                p["state"] = "rejected"
                await report_decision(
                    self.host.runtime, p["decision"], "rejected", "executor_declined", p["turn"]
                )
                return {"state": "rejected"}
            p["state"] = "applying"  # Claim before IO. Lost replies never replay operations.
            try:
                result = await self.apply(p)
                p["state"] = result["state"]
            except asyncio.CancelledError:
                p["state"] = "unknown"
                await report_decision(
                    self.host.runtime, p["decision"], "unknown", "confirmation_cancelled", p["turn"]
                )
                raise
            except Exception:
                p["state"] = "unknown"
                result = {"state": "unknown", "message": "交接状态尚未确认，不重复执行；请核对当前操作。"}
            await report_decision(
                self.host.runtime,
                p["decision"],
                "adopted" if p["state"] == "applied" else "unknown",
                "executor_confirmed" if p["state"] == "applied" else "handoff_unconfirmed",
                p["turn"],
            )
            return result

    async def apply(self, p):
        h = self.host
        if len(p["task_ids"]) > 1:
            return {"state": "clarification", "message": "关联多个任务，请明确要调整哪一个。"}
        if not p["task_ids"]:
            # A matching foreground checkpoint lets the SAME executor continue its
            # corrected turn; it does not start or replay another execution.
            if not p["turn"] or not await h.backend.control_checkpoint(p["turn"]):
                return {"state": "unknown", "message": "无法确认当前执行的交接点，不启动替代操作。"}
            if not self.current(p):
                return {"state": "expired"}
            return {"state": "applied", "message": "当前执行已确认新意图，请沿用本轮处理；不要创建重复任务。"}
        task = p["task_ids"][0]
        page = await h.runtime.invoke(TaskRequest("get", {"task_id": task}), timeout=2)
        runs = page.get("active_runs", [])
        if len(runs) != 1 or runs[0]["id"] != p["runs"].get(task):
            return {"state": "clarification", "message": "没有唯一的活动执行，请先核对目标。"}
        run = runs[0]
        if not self.current(p):
            return {"state": "expired"}
        if p["choice"] == "supplement":
            await h.runtime.invoke(
                TaskRequest("supplement", {"run_id": run["id"], "text": p["text"]}), timeout=10
            )
        else:
            stopped = await h.runtime.invoke(TaskRequest("pause_run", {"run_id": run["id"]}), timeout=15)
            if stopped["state"] != "paused":
                return {"state": "unknown", "message": "旧执行停止未确认，不启动替代操作。"}
            if not self.current(p):
                return {"state": "expired"}
            # Existing continuation keeps task identity, permissions and prior evidence.
            await h.runtime.invoke(
                TaskRequest("continue", {"run_id": run["id"], "text": p["text"]}), timeout=10
            )
        return {
            "state": "applied",
            "run_id": run["id"],
            "message": "已交接到关联任务，不要再次发送相同要求。",
        }

    async def stop(self, turn, current=lambda: True):
        if not current():
            return False
        proposal = self.pending
        if proposal and proposal["turn"] == turn:
            # Invalidate before IO: a concurrent correction must not resume after takeover.
            self.pending = None
            await report_decision(
                self.host.runtime, proposal["decision"], "rejected", "operation_stopped", turn
            )
        try:
            return await self._stop(turn, current)
        except Exception:
            return False  # Never interpret an interrupted RPC as a verified stop.

    async def _stop(self, turn, current):
        """Stop a uniquely associated execution, never archive its schedule."""
        h = self.host
        tasks = tuple(self.links.get(turn, ()))
        if not current() or len(tasks) > 1:
            return False
        if tasks:
            page = await h.runtime.invoke(TaskRequest("get", {"task_id": tasks[0]}), timeout=2)
            runs = page.get("active_runs", [])
            if len(runs) != 1 or runs[0]["id"] != self.links.get(turn, {}).get(tasks[0]) or not current():
                return False
            result = await h.runtime.invoke(TaskRequest("cancel_run", {"run_id": runs[0]["id"]}), timeout=15)
            return result["state"] == "canceled"
        if not turn or h.backend.active_turn != turn:
            return False
        await h.backend.cancel(expected_turn=turn)
        for _ in range(5):
            if not current():
                return False
            if await h.backend.control_checkpoint(turn, stopped=True):
                return True
            await asyncio.sleep(0.1)
        return False
