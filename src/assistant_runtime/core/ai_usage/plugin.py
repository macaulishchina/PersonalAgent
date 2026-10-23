"""Host-owned identity and query boundary for AI usage, not action permissions."""

from .declaration import DECLARATION

import asyncio

from assistant_runtime.api.ai_usage import UsageRequest, UsageScope, UsageError, current_usage
from assistant_runtime.api.contracts import NotificationRequest, ConversationFeedbackRequest
from assistant_runtime.core.ai_usage.service import UsageService, independent_scope
from assistant_runtime.core.ai_usage.settings import UsageSettings


class AIUsagePlugin:
    manifest = DECLARATION.manifest

    def __init__(self, host):
        self.host = host
        self.service = UsageService(
            host.config.state_dir / "ai-usage", UsageSettings.parse(host.config.ai_usage)
        )

    async def start(self, context):
        self.context = context
        context.register(UsageRequest, self.request)
        context.own(self.service.close)
        await self.service.start()
        context.spawn(self.maintenance(), "ledger-and-notices")

    def diagnostics(self):
        return {
            "ready": self.service.ready,
            "error": self.service.error,
            "pending": self.service.pending,
            "configuration_revision": self.service.settings.revision(),
        }

    async def scope(self, caller_run=None, *, independent=None):
        if independent:
            return independent_scope(independent)
        if caller_run:
            bound = await self.service.binding(caller_run)
            if bound:
                inherited = current_usage.get()
                if inherited and inherited.work_id == bound.work_id:
                    from dataclasses import replace

                    return replace(
                        bound,
                        parent_call=inherited.parent_call,
                        execution_version=inherited.execution_version,
                    )
                return bound
            from assistant_runtime.api.contracts import TaskRequest

            info = await self.host.runtime.invoke(TaskRequest("execution_info", {"run_ids": [caller_run]}))
            run = info.get(caller_run, {})
            if run.get("state") not in {"queued", "dispatching", "running", "waiting_user"}:
                raise UsageError("usage_inactive_caller")
            return await self.service.bind(
                caller_run, UsageScope(caller_run, caller_run, run.get("task_id", caller_run))
            )
        inherited = current_usage.get()
        if inherited:
            return inherited
        return independent_scope("auxiliary")

    async def bind_task(self, run, parent=None):
        if await self.service.binding(run["id"]):
            return
        if (run["data"].get("ai_usage") or {}).get("requests", 0):
            raise UsageError("usage_missing_ledger", (run["data"].get("ai_usage") or {}).get("work_id"))
        scope = parent or UsageScope(run["id"], run["id"], run["task_id"])
        await self.service.bind(
            run["id"],
            UsageScope(scope.work_id, run["id"], run["task_id"], scope.parent_call, scope.execution_version),
        )

    async def request(self, request):
        action, args = request.action, request.arguments
        if action == "status":
            return self.diagnostics()
        if action == "extend":
            if request.caller_run is not None or current_usage.get() is not None:
                raise ValueError("Only explicit user maintenance may extend usage")
            if set(args) != {"work_id", "requests", "reason"}:
                raise ValueError("Usage extension requires work_id,requests,reason")
            return await self.service.call("extend", args["work_id"], args["requests"], args["reason"])
        if action == "summary":
            scope = (
                await self.scope(request.caller_run) if request.caller_run or current_usage.get() else None
            )
            work = scope.work_id if scope else args.get("work_id")
            if not isinstance(work, str):
                raise ValueError("Usage summary requires work_id")
            return await self.service.snapshot(work)
        if action not in {"calls", "stats"}:
            raise ValueError("Unknown usage action")
        filters = dict(args)
        if request.caller_run:
            scope = await self.scope(request.caller_run)
            filters.update(work_id=scope.work_id, task_id=scope.task_id)
        result = await self.service.call("query", filters)
        # Neither MCP nor routine queries export diagnostic request/response bodies.
        for item in result["items"]:
            item.pop("request_body", None)
            item.pop("response_body", None)
        return result

    async def maintenance(self):
        ticks = 0
        while True:
            if self.service.ready:
                try:
                    if ticks % 3600 == 0:
                        await self.service.call("prune")
                    for notice in await self.service.call("notices"):
                        if notice["kind"] == "blocked":
                            tasks = self.host.runtime.plugins.get("tasks")
                            owners = await tasks.usage_blocked(notice) if tasks else []
                            payload = {
                                "event_key": notice["id"],
                                "group_key": "ai-usage:" + notice["work_id"],
                                "title": "AI 调用已暂停",
                                "kind": "attention",
                                "priority": "important",
                                "voice": False,
                                "body": "本次工作达到调用预算或无法核算已启用的预算。原因："
                                + notice["reason"]
                                + "。已发出的操作按实际结果收尾；请查看任务或调用记录，明确调整额度后继续。",
                            }
                            claimed = next(
                                (
                                    o
                                    for o in owners
                                    if o["feedback"]["owner"] == "conversation"
                                    and o["feedback"]["state"] == "open"
                                ),
                                None,
                            )
                            delivered = False
                            if claimed:
                                run = claimed["run"]
                                receipt = await self.context.runtime.invoke(
                                    ConversationFeedbackRequest(
                                        "deliver",
                                        claimed["feedback"]["context"],
                                        {
                                            "id": notice["id"],
                                            "task_id": run["task_id"],
                                            "run_id": run["id"],
                                            "payload": payload,
                                            "attachments": [],
                                        },
                                    )
                                )
                                delivered = receipt["state"] in {"delivered", "submitted"}
                            if not delivered:
                                await self.context.runtime.invoke(
                                    NotificationRequest(
                                        "publish", payload, owners[0]["run"]["id"] if owners else None
                                    )
                                )
                        await self.service.call("acknowledge", notice["id"])
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # Durable notice remains pending; no paid call is used for recovery.
                    pass
            else:
                try:
                    await self.context.runtime.invoke(
                        NotificationRequest(
                            "publish",
                            {
                                "event_key": "ai-usage:unavailable:" + self.service.settings.revision(),
                                "group_key": "ai-usage:health",
                                "title": "AI 调用计量暂不可用",
                                "voice": False,
                                "body": "无法可靠登记调用，新的 AI 服务请求已暂停；设备、网页及其他功能继续运行。请检查计量存储与服务状态。",
                            },
                        )
                    )
                except Exception:
                    pass
            ticks += 1
            await asyncio.sleep(1)
