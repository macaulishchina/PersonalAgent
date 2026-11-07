"""Per-terminal receipts on canonical notices; no duplicate task state."""

import time
from assistant_runtime.api.ai_usage import current_usage


class TerminalDelivery:
    def __init__(self, plugin):
        self.plugin, self.repo = plugin, plugin.repo
        self.db = self.repo.db
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS notification_terminal_receipts(notification TEXT, revision INTEGER, endpoint TEXT, received INTEGER DEFAULT 0, played INTEGER DEFAULT 0, PRIMARY KEY(notification,revision,endpoint))"
        )
        self.db.commit()

    def belongs(self, item, session):
        origin = item["payload"].get("origin", {})
        return origin.get("conversation_id") == session and item["source"] == "task:" + str(
            origin.get("task_id")
        )

    async def request(self, request):
        if current_usage.get() is not None:
            raise PermissionError("Terminal delivery is not an execution tool")
        if not request.endpoint_id or len(request.endpoint_id) > 128:
            raise ValueError("Invalid endpoint")
        args = request.arguments
        if request.action == "list":
            limit = args.get("limit", 20)
            if type(limit) is not int or not 1 <= limit <= 50:
                raise ValueError("Invalid notification page")
            before = args.get("before")
            if before is None:
                stamp, cursor_id = time.time() + 1, ""
            elif (
                isinstance(before, list)
                and len(before) == 2
                and type(before[0]) in (float, int)
                and isinstance(before[1], str)
            ):
                stamp, cursor_id = before
            else:
                raise ValueError("Invalid notification cursor")
            rows = self.db.execute(
                "SELECT id FROM notifications WHERE json_extract(payload,'$.origin.conversation_id')=? AND source='task:'||json_extract(payload,'$.origin.task_id') AND (updated<? OR (updated=? AND id<?)) AND coalesce(json_extract(payload,'$.kind'),'attention')!='progress' ORDER BY updated DESC,id DESC LIMIT ?",
                (request.session_id, stamp, stamp, cursor_id, limit),
            ).fetchall()
            items = [self.repo.get(row[0]) for row in rows]
            items = [item for item in items if self.belongs(item, request.session_id)]
            for item in items:
                receipt = self.db.execute(
                    "SELECT received,played FROM notification_terminal_receipts WHERE notification=? AND revision=? AND endpoint=?",
                    (item["id"], item["revision"], request.endpoint_id),
                ).fetchone()
                item["terminal_receipt"] = {
                    "received": bool(receipt and receipt[0]),
                    "played": bool(receipt and receipt[1]),
                }
            from assistant_runtime.core.speech.quiet import QuietHours

            config = self.plugin.host.config
            return {
                "items": items,
                "before": [items[-1]["updated"], items[-1]["id"]] if len(items) == limit else None,
                "quiet": bool((quiet := QuietHours.from_settings(config.quiet_hours)) and quiet.active()),
            }
        item = self.repo.get(args.get("id"))
        if not item or not self.belongs(item, request.session_id) or item["revision"] != args.get("revision"):
            raise ValueError("Unknown or changed terminal notification")
        if request.action == "get":
            payload = item["payload"]
            text = payload.get("body") or payload.get("summary") or payload.get("title", "任务结果")
            reference = (
                payload.get("result_ref")
                if (payload.get("presentation") or {}).get("body") == "excerpt"
                else None
            )
            offset, limit = args.get("offset", 0), args.get("limit", 240)
            if (
                type(offset) is not int
                or offset < 0
                or (not reference and offset > len(text))
                or type(limit) is not int
                or not 1 <= limit <= 1000
            ):
                raise ValueError("Invalid notification page")
            usage = getattr(self.plugin.host, "ai_usage", None)
            scope = await usage.service.binding(payload["origin"].get("run_id")) if usage else None
            end = min(len(text), offset + limit)
            fragment, following = text[offset:end], end if end < len(text) else None
            if reference:
                from assistant_runtime.api.files import FileRequest

                page = await self.plugin.host.runtime.invoke(
                    FileRequest(
                        "result_read",
                        {
                            "id": reference["id"],
                            "version": reference["version"],
                            "offset": offset,
                            "length": limit,
                        },
                    )
                )
                fragment, following = page["data"], None if page["eof"] else page["next_offset"]
            return {
                "notification_id": item["id"],
                "notification_revision": item["revision"],
                "id": "notification:" + item["id"],
                "text": fragment,
                "updated": item["updated"],
                "offset": offset,
                "next_offset": following,
                "work_id": scope.work_id if scope else None,
                "result_ref": payload.get("result_ref"),
            }
        if request.action in {"received", "played"}:
            with self.db:
                self.db.execute(
                    "INSERT OR IGNORE INTO notification_terminal_receipts(notification,revision,endpoint) VALUES(?,?,?)",
                    (item["id"], item["revision"], request.endpoint_id),
                )
                column = request.action
                self.db.execute(
                    f"UPDATE notification_terminal_receipts SET {column}=1 WHERE notification=? AND revision=? AND endpoint=?",
                    (item["id"], item["revision"], request.endpoint_id),
                )
        elif request.action == "read":
            self.repo.mark_read(item["id"], item["revision"])
        else:
            raise ValueError("Unknown terminal receipt")
        return {"ok": True}
