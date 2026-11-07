"""Observe linked owner results without a second business ledger or execution loop."""

import json

from assistant_runtime.api.operations import summarize, validate_observations


LABELS = {
    "pending": "业务结果仍在核验",
    "unknown": "业务结果尚不能确认",
    "waiting_user": "需要你补充信息或处理授权",
    "partial": "业务操作部分完成",
    "failed": "业务操作未完成",
    "cancelled": "业务操作已取消",
    "completed": "业务操作已核验完成",
}


class BusinessResults:
    def __init__(self, plugin):
        self.plugin = plugin

    def event_state(self, run):
        """Dependent tasks must not consume Agent prose as business completion."""
        if run["state"] == "completed":
            if getattr(self.plugin.host, "operation_sources", {}) and not run["data"].get(
                "business_observed"
            ):
                return None
            state = (run["data"].get("business_result") or {}).get("state")
            if state in {"pending", "unknown", "waiting_user"}:
                return None
            if state in {"failed", "partial", "cancelled"}:
                return "failed"
        return run["state"]

    async def refresh(self, run):
        sources = getattr(self.plugin.host, "operation_sources", {})
        prior = run["data"].get("business_result") or {}
        observations, issues = {}, []
        known = set(prior.get("sources", {}))
        for (raw,) in self.plugin.repo.db.execute(
            "SELECT metadata FROM job_tool_trace WHERE run_id=?", (run["id"],)
        ):
            trace = json.loads(raw)
            if trace.get("operation_id") and trace.get("server") in sources:
                known.add(trace["server"])
        for owner, request in sources.items():
            try:
                result = await self.plugin.host.runtime.invoke(
                    request("operation_outcomes", {"run_id": run["id"]}), timeout=2
                )
                validate_observations(result)
                if result["operations"]:
                    previous = {item["id"]: item for item in prior.get("sources", {}).get(owner, [])}
                    latest = {item["id"]: item for item in result["operations"]}
                    if previous.keys() - latest.keys() or any(
                        item["revision"] < previous[item["id"]]["revision"]
                        for item in result["operations"]
                        if item["id"] in previous
                    ):
                        raise ValueError("Operation evidence disappeared or moved backwards")
                    observations[owner] = result["operations"]
                elif owner in known:
                    issues.append(owner)
            except Exception:
                # An unrelated optional plugin failing must not block ordinary tasks.
                if owner in known:
                    issues.append(owner)
        issues.extend(sorted(known - set(sources)))
        for owner in issues:
            observations[owner] = [
                {
                    **item,
                    "state": "unknown",
                    "responsibility": "system",
                    "code": "operation_provider_unavailable",
                }
                for item in prior.get("sources", {}).get(owner, [])
            ]
        latest_run = self.plugin.repo.run(run["id"])
        if latest_run["revision"] != run["revision"]:
            return latest_run  # A late observation cannot overwrite a newer execution round/progress.
        if not observations and not issues:
            return (
                self.plugin.repo.update_run(run["id"], business_observed=True)
                if sources and not run["data"].get("business_observed")
                else run
            )
        items = [item for values in observations.values() for item in values]
        summary = summarize(items)
        if issues:
            summary["state"] = "unknown"
        value = {**summary, "sources": observations, "unavailable": sorted(set(issues))}
        if value != prior:
            summary_run = {**run, "data": {**run["data"], "business_result": value}}
            original = run["data"].get("execution_summary") or run["data"].get("progress", "")
            report = self.report(summary_run)
            run = self.plugin.repo.update_run(
                run["id"],
                business_result=value,
                business_observed=True,
                execution_summary=original,
                progress=report or original,
                business_feedback_pending=bool(run["data"].get("completion_delivered")),
            )
        return run

    @staticmethod
    def unsettled(run):
        return (run["data"].get("business_result") or {}).get("state") in {
            "pending",
            "unknown",
            "waiting_user",
        }

    @staticmethod
    def report(run):
        value = run["data"].get("business_result")
        if not value or value["state"] == "completed":
            return None
        lines = ["执行已结束，" + LABELS[value["state"]] + "。"]
        counts = value.get("counts", {})
        lines.append("；".join(f"{LABELS[state]} {count} 项" for state, count in counts.items()))
        for owner, items in value["sources"].items():
            if len(lines) >= 8:
                break
            for item in items:
                if item["state"] != "completed":
                    text = item.get("summary") or LABELS[item["state"]]
                    action = item.get("next_action", "")
                    if item["responsibility"] == "user":
                        lines.append(text + ("：" + action if action else ""))
                    elif text not in lines:
                        lines.append(text)
                    if len(lines) >= 8:
                        break
        if value.get("unavailable"):
            lines.append("结果所属插件暂不可用，操作证据保留；未重新执行。")
        elif value["state"] in {"unknown", "pending"}:
            lines.append("这是系统核验事项，不需要你逐项确认；未自动重做已提交操作。")
        return "\n\n".join(line for line in lines if line)
