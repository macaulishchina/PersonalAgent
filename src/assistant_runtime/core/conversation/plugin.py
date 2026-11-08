"""One public registry; entrances bind stable views, never executor processes."""

from assistant_runtime.api.conversation import ConversationInputChanged

import asyncio
import uuid
from assistant_runtime.api.conversation import (
    ConversationRequest,
    ConversationInputRequest,
    current_conversation,
)
from assistant_runtime.api.contracts import ConversationFeedbackRequest
from assistant_runtime.api.ai_usage import current_usage
from .declaration import DECLARATION
from .sessions import Sessions
from .service import Conversation


class Conversations:
    manifest = DECLARATION.manifest

    def __init__(self, host):
        self.host = host
        self.sessions = Sessions(host.store)
        host.backend.session_id = self.sessions.bound("web")
        if host.assistant is None:
            self.main = Conversation(host)
        else:
            from assistant_runtime.core.speech.conversation import AssistantConversation

            self.main = AssistantConversation(host.assistant)
        host.conversation = self.main
        host.conversations = self
        self.opened = {self.main.session: self.main}
        self.opening = asyncio.Lock()
        self.context = None
        from .snapshots import Snapshots

        self.snapshots = Snapshots(self)
        from .inputs import EntranceInputs

        self.inputs = EntranceInputs(self)

    async def start(self, context):
        self.context = context
        for row in self.sessions.db.execute("SELECT id FROM conversation_sessions").fetchall():
            self.main.store.recover(row[0])
        await self.main.start(context, register=False)
        context.register(ConversationRequest, self.request)
        context.register(ConversationInputRequest, self.inputs.request)
        context.register(ConversationFeedbackRequest, self.feedback)

    async def get(self, ident):
        self.sessions.get(ident)
        async with self.opening:
            if ident not in self.opened:
                # A report delivery may open a view inside a task's call scope. Persistent
                # conversation workers must not inherit that task's metering or identity.
                usage_token = current_usage.set(None)
                session_token = current_conversation.set(None)
                try:
                    backend = await self.host.backend.open_conversation(ident)
                    chat = Conversation(self.host, backend)
                    await chat.start(self.context, register=False)
                    self.opened[ident] = chat
                finally:
                    current_conversation.reset(session_token)
                    current_usage.reset(usage_token)
        return self.opened[ident]

    async def feedback(self, request):
        if request.action == "capture":
            ident = current_conversation.get()
            if not ident and current_usage.get() is not None:
                return None
            chat = self.opened.get(ident or self.main.session)
            return chat.task_replies.current() if chat else None
        chat = await self.get((request.context or {}).get("session"))
        return await chat.task_replies.request(request)

    async def request(self, request):
        action, args = request.action, request.arguments
        allowed = {
            "create": {"title", "request_id", "policy"},
            "list": {"before", "limit"},
            "open": {"operator", "session_id"},
            "history": {"session_id", "before"},
            "history_index": {"session_id", "before", "after", "limit"},
            "message": {"session_id", "message_id", "offset", "limit"},
            "thumbnail": {"session_id", "message_id", "image_id", "width", "height"},
            "events": {"session_id", "cursor"},
            "state": {"session_id"},
            "receipt": {"session_id", "request_id", "include_execution"},
            "receipts": {"session_id", "request_ids", "include_execution"},
            "submit": {"session_id", "request_id", "text", "attachments", "expected_model"},
            "queue": {"session_id"},
            "edit_queued": {"session_id", "request_id", "revision", "text"},
            "remove_queued": {"session_id", "request_id", "revision"},
            "reorder_queue": {"session_id", "request_ids", "expected_order"},
            "cancel": {"session_id", "request_id"},
            "retry": {"session_id", "request_id", "turn_id"},
            "supplement": {"session_id", "request_id", "text"},
            "answer": {"session_id", "request_id", "question_id", "text"},
            "snapshot": {"session_id", "request_id"},
            "snapshot_receipt": {"session_id", "request_id"},
            "snapshot_inputs": {"session_id", "snapshot_ref", "limit", "cursor"},
        }.get(action)
        if allowed is None or not isinstance(args, dict) or set(args) - allowed:
            raise ValueError("Invalid conversation operation")
        bound = current_conversation.get()
        if current_usage.get() is not None and (
            not bound or action in {"create", "list", "open"} or args.get("session_id") != bound
        ):
            raise ValueError("Conversation operation is outside the bound execution")
        if action == "create":
            return self.sessions.create(
                args.get("title", "New conversation"), args.get("request_id"), args.get("policy")
            )
        if action == "list":
            return self.sessions.list(args.get("before"), args.get("limit", 50))
        if action == "open":
            operator = args.get("operator")
            if not isinstance(operator, str) or not operator or len(operator) > 128:
                raise ValueError("An operator identity is required")
            entrance = "cli:" + operator
            ident = args.get("session_id") or self.sessions.bound(entrance)
            if ident is None:
                ident = self.sessions.create("CLI")["id"]
            self.sessions.bind(entrance, ident)
            return self.sessions.get(ident)
        ident = args.get("session_id")
        self.sessions.get(ident)
        if action in {"snapshot", "snapshot_receipt", "snapshot_inputs"}:
            return await self.snapshots.request(action, args)
        store = self.main.store
        if action in {"message", "thumbnail"}:
            item = store.get(args.get("message_id"))
            offset, limit = args.get("offset", 0), args.get("limit", 240)
            if not item or item["session_id"] != ident:
                raise ValueError("Unknown conversation message")
            images = [ref for ref in item["attachments"] if ref.get("type", "").startswith("image/")][:4]
            if action == "thumbnail":
                width, height = args.get("width", 160), args.get("height", 96)
                if (
                    type(width) is not int
                    or not 16 <= width <= 160
                    or type(height) is not int
                    or not 16 <= height <= 96
                    or args.get("image_id") not in {r["id"] for r in images}
                ):
                    raise ValueError("Invalid conversation image request")
                from assistant_runtime.core.workfiles.thumbnails import render

                raw = self.main.media.image_bytes(args["image_id"])
                # The owning media registry validates file identity and bounds.
                # This worker only processes registered bytes, never paths/URLs.
                result = await asyncio.to_thread(render, raw, width, height)
                return {"image_id": args["image_id"], **result}
            presentation = item.get("presentation") or {}
            reference = item.get("result_ref")
            total = (
                presentation.get("result_length")
                if reference and presentation.get("body") == "excerpt"
                else len(item["text"])
            )
            if type(total) is not int:
                total = len(item["text"])
            if (
                type(offset) is not int
                or not 0 <= offset <= total
                or type(limit) is not int
                or not 1 <= limit <= 1000
            ):
                raise ValueError("Invalid message page")
            end = min(offset + limit, total)
            text = item["text"][offset:end]
            if reference and presentation.get("body") == "excerpt":
                from assistant_runtime.api.files import FileRequest

                page = await self.host.runtime.invoke(
                    FileRequest(
                        "result_read",
                        {
                            "id": reference["id"],
                            "version": reference["version"],
                            "offset": presentation.get("result_offset", 0) + offset,
                            "length": max(1, end - offset),
                        },
                    )
                )
                text = page["data"][: end - offset]
                end = offset + len(text)
            row = store.db.execute(
                "SELECT id FROM conversation_queue WHERE session_id=? AND turn_id=?",
                (ident, item.get("turn_id")),
            ).fetchone()
            work = (
                await self.host.ai_usage.service.binding(row[0])
                if row and getattr(self.host, "ai_usage", None)
                else None
            )
            return {
                "work_id": work.work_id if work else None,
                "message_id": item["id"],
                "role": item["role"],
                "text": text,
                "total": total,
                "offset": offset,
                "next_offset": end if end < total else None,
                "updated": item["updated"],
                "status": item["status"],
                "kind": item["kind"],
                "result_ref": item.get("result_ref"),
                "images": images,
            }
        if "include_execution" in args and type(args["include_execution"]) is not bool:
            raise ValueError("include_execution must be boolean")
        if action == "receipts":
            ids = args.get("request_ids")
            if not isinstance(ids, list) or len(ids) > 100 or any(not isinstance(i, str) for i in ids):
                raise ValueError("Expected at most 100 request IDs")
            ids = list(dict.fromkeys(str(uuid.UUID(i)) for i in ids))
            if not ids:
                return {}
            marks = ",".join("?" for _ in ids)
            rows = store.db.execute(
                f"SELECT r.id,q.state FROM chat_requests r JOIN conversation_queue q ON q.id=r.id WHERE r.session_id=? AND r.id IN ({marks})",
                (ident, *ids),
            ).fetchall()
            found = dict(rows)
            return {
                i: {
                    "found": i in found,
                    "state": found.get(i),
                    **(
                        {"execution": store.execution(i)}
                        if args.get("include_execution") and i in found
                        else {}
                    ),
                }
                for i in ids
            }
        if action == "receipt":
            row = store.db.execute(
                "SELECT q.state,r.message_id FROM chat_requests r JOIN conversation_queue q ON q.id=r.id WHERE r.id=? AND r.session_id=?",
                (args.get("request_id"), ident),
            ).fetchone()
            return {
                "found": bool(row),
                "state": row[0] if row else None,
                "message": store.get(row[1]) if row else None,
                **(
                    {"execution": store.execution(args.get("request_id"))}
                    if args.get("include_execution") and row
                    else {}
                ),
            }
        if action == "history_index":
            return store.history_index(
                ident, before=args.get("before"), after=args.get("after"), limit=args.get("limit", 4)
            )
        if action == "history":
            messages = store.history(ident, args.get("before"))
            return {
                "messages": messages,
                "cursor": store.cursor(),
                "before": messages[0]["ordinal"] if len(messages) == 60 else None,
            }
        if action == "events":
            cursor = args.get("cursor", 0)
            if type(cursor) is not int or cursor < 0:
                raise ValueError("Invalid event cursor")
            events = store.changes(ident, cursor)
            return {"events": events, "cursor": events[-1]["seq"] if events else store.cursor()}
        if action == "queue":
            return {"requests": store.queue(ident)}
        if action == "state" and ident not in self.opened:
            return {
                "session_id": ident,
                "task_running": False,
                "configuration_required": not self.host.backend.client.settings.enabled,
            }
        chat = await self.get(ident)
        if action == "state":
            return chat.state()
        if action == "submit":
            return await chat.submit(
                args.get("request_id"),
                args.get("text"),
                args.get("attachments", []),
                source="cli",
                **({"expected_model": args["expected_model"]} if "expected_model" in args else {}),
            )
        if action == "retry":
            if not isinstance(args.get("turn_id"), str) or not args["turn_id"]:
                raise ValueError("需要提供失败执行标识")
            return await chat.submit(
                args.get("request_id"),
                "继续上一轮未完成的请求，沿用已确认的操作结果。",
                [],
                source="operator",
                retry_of=args["turn_id"],
            )
        if action == "cancel":
            return await chat.cancel_request(args.get("request_id"))
        if action == "edit_queued":
            return chat.edit_queued(args.get("request_id"), args.get("revision"), args.get("text"))
        if action == "remove_queued":
            return chat.remove_queued(args.get("request_id"), args.get("revision"))
        if action == "reorder_queue":
            return chat.reorder_queue(args.get("request_ids"), args.get("expected_order"))
        return await self.active_input(chat, action, args)

    async def active_input(self, chat, action, args, *, source="operator"):
        """Shared dispatch for operator and identity-bound entrance supplements."""
        ident, store = chat.session, chat.store
        row = store.db.execute(
            "SELECT state,turn_id FROM conversation_queue WHERE id=? AND session_id=?",
            (args.get("request_id"), ident),
        ).fetchone()
        if not row or row[0] != "running" or row[1] != chat.backend.active_turn:
            raise ConversationInputChanged("Request is no longer the active conversation turn")
        text = args.get("text")
        if not isinstance(text, str) or not text.strip() or len(text) > 32000:
            raise ValueError("A bounded nonempty input is required")
        result = await chat.backend.conversation_input(action, text, row[1], args.get("question_id"))
        chat.store.put(
            "input:" + uuid.uuid4().hex,
            ident,
            role="user",
            source=source,
            text=text,
            status="complete",
            turn_id=row[1],
        )
        return result

    async def maintenance_cancel(self):
        for chat in self.opened.values():
            for item in chat.queue():
                await chat.cancel_request(item["id"])

    def maintenance_pending(self):
        return [
            {"id": r[0], "state": r[1]}
            for r in self.sessions.db.execute(
                "SELECT id,state FROM conversation_queue WHERE state IN ('queued','sending','running')"
            )
        ]
