"""Owner-side observations; no network access, model inference or cross-plugin reads."""

import json

from assistant_runtime.api.operations import summarize
from .errors import MESSAGES


def for_run(repo, arguments, caller):
    if set(arguments) != {"run_id"}:
        raise ValueError("Operation observations require a run identity")
    run = arguments["run_id"]
    if not isinstance(run, str) or not 1 <= len(run) <= 200 or caller and caller != run:
        raise PermissionError("Cannot inspect another execution's operation outcomes")
    rows = repo.db.execute(
        "SELECT id,state,data FROM media_operations WHERE json_extract(data,'$.caller_run')=? "
        "ORDER BY created,id LIMIT 1001",
        (run,),
    ).fetchall()
    if len(rows) > 1000:
        raise ValueError("Operation observation limit exceeded")
    items = []
    for ident, state, raw in rows:
        data = json.loads(raw)
        code = data.get("error") or ""
        outcome = (
            "completed"
            if state == "completed"
            else "failed"
            if state == "failed"
            else "cancelled"
            if state == "cancelled"
            else "unknown"
            if state == "uncertain"
            else "waiting_user"
            if state == "waiting_auth"
            else "pending"
        )
        labels = {
            "completed": "已核验完成",
            "failed": "失败",
            "cancelled": "已取消",
            "unknown": "结果尚未确认",
            "waiting_user": "需要账号授权",
            "pending": "仍在处理中",
        }
        summary = MESSAGES.get(code, "媒体操作" + labels[outcome] + ("：" + code if code else ""))
        field = data.get("verification_issue", {}).get("field")
        if field in {"file_id", "parent_file_id", "name", "type", "size", "content_hash", "trashed_at"}:
            summary += " 核验字段：" + field + "。"
        if outcome == "unknown" and data.get("observation_attempts", 0) >= 3:
            summary += " 三次只读核对仍未确认，已停止自动核对；保留回执供操作者核查。"
        items.append(
            {
                "id": ident,
                "revision": data.get("observation_revision", 0),
                "state": outcome,
                "responsibility": "user"
                if state == "waiting_auth"
                else "system"
                if outcome in {"unknown", "pending"}
                else "none",
                "code": code,
                "summary": summary,
                "next_action": "更新对应网盘账号凭据" if state == "waiting_auth" else "",
            }
        )
    return {"operations": items, **summarize(items)}
