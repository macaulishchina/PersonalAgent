"""One public registry; entrances bind stable views, never executor processes."""

import asyncio
import uuid
from assistant_runtime.api.conversation import ConversationRequest, current_conversation
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

    async def start(self, context):
        self.context = context
        for row in self.sessions.db.execute("SELECT id FROM conversation_sessions").fetchall():
            self.main.store.recover(row[0])
        await self.main.start(context, register=False)
        context.register(ConversationRequest, self.request)
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
            "create": {"title"},
            "list": {"before", "limit"},
            "open": {"operator", "session_id"},
            "history": {"session_id", "before"},
            "events": {"session_id", "cursor"},
            "state": {"session_id"},
            "submit": {"session_id", "request_id", "text", "attachments"},
            "queue": {"session_id"},
            "cancel": {"session_id", "request_id"},
            "supplement": {"session_id", "request_id", "text"},
            "answer": {"session_id", "request_id", "question_id", "text"},
        }.get(action)
        if allowed is None or not isinstance(args, dict) or set(args) - allowed:
            raise ValueError("Invalid conversation operation")
        bound = current_conversation.get()
        if current_usage.get() is not None and (
            not bound or action in {"create", "list", "open"} or args.get("session_id") != bound
        ):
            raise ValueError("Conversation operation is outside the bound execution")
        if action == "create":
            return self.sessions.create(args.get("title", "New conversation"))
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
        store = self.main.store
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
            return {
                "requests": [
                    {"id": r[0], "state": r[1]}
                    for r in store.db.execute(
                        "SELECT id,state FROM conversation_queue WHERE session_id=? AND state IN ('queued','sending','running') ORDER BY ordinal",
                        (ident,),
                    )
                ]
            }
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
                args.get("request_id"), args.get("text"), args.get("attachments", []), source="cli"
            )
        if action == "cancel":
            return await chat.cancel_request(args.get("request_id"))
        row = store.db.execute(
            "SELECT state,turn_id FROM conversation_queue WHERE id=? AND session_id=?",
            (args.get("request_id"), ident),
        ).fetchone()
        if not row or row[0] != "running" or row[1] != chat.backend.active_turn:
            raise ValueError("Request is no longer the active conversation turn")
        text = args.get("text")
        if not isinstance(text, str) or not text.strip() or len(text) > 32000:
            raise ValueError("A bounded nonempty input is required")
        result = await chat.backend.conversation_input(action, text, row[1], args.get("question_id"))
        chat.store.put(
            "input:" + uuid.uuid4().hex,
            ident,
            role="user",
            source="operator",
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
