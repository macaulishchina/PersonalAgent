"""Conversation-owned task result projection and once-only speech submission receipts."""

import json

from assistant_runtime.api.contracts import ConversationFeedbackRequest
from assistant_runtime.core.conversation.feedback import spoken_result


class TaskReplies:
    def __init__(self, conversation):
        self.conversation = conversation
        self.db = conversation.store.db
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS chat_result_receipts (
          id TEXT PRIMARY KEY, session TEXT NOT NULL, state TEXT NOT NULL, message_id TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS chat_result_closed_turns (turn TEXT PRIMARY KEY);
        """)

    def close(self, turn):
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO chat_result_closed_turns VALUES(?)", (turn,))

    def current(self):
        c = self.conversation
        turn = c.backend.active_turn
        receipt = c.feedback.get(turn) if turn else None
        if not receipt or receipt["state"] != "bound":
            return None
        if self.db.execute("SELECT 1 FROM chat_result_closed_turns WHERE turn=?", (turn,)).fetchone():
            return None
        if receipt["source"] == "voice" and not c.feedback_allowed(turn):
            return None
        return {
            "session": c.session,
            "turn": turn,
            "binding": json.dumps([receipt["source"], receipt["epoch"], receipt["revision"]]),
        }

    async def request(self, request: ConversationFeedbackRequest):
        if request.action == "capture":
            return self.current()
        if request.action != "deliver":
            raise ValueError("Unknown conversation feedback action")
        c, result = self.conversation, request.result
        ident = result["id"]
        saved = self.db.execute(
            "SELECT state,message_id FROM chat_result_receipts WHERE id=?", (ident,)
        ).fetchone()
        if saved:
            # Interrupted RPC/output is never permission to speak again.
            return {"state": "unknown" if saved[0] == "submitting" else saved[0], "message_id": saved[1]}
        context = request.context or {}
        receipt = c.feedback.get(context.get("turn"))
        durable = bool(
            receipt
            and receipt["source"] != "voice"
            and context.get("session") == c.session
            and context.get("binding")
            == json.dumps([receipt["source"], receipt["epoch"], receipt["revision"]])
        )
        if not durable and request.context != self.current():
            return {"state": "released"}
        receipt = c.feedback.get(context["turn"])
        voice = receipt["source"] == "voice"
        payload = result["payload"]
        text = payload["title"] + "\n\n" + payload["body"]
        attachments = c.media.resolve(result.get("attachments", []))
        # No await between validating live ownership, durable projection and receipt.
        c.store.put(
            ident,
            context["session"],
            source="task",
            kind="task",
            status="complete",
            text=text,
            turn_id=ident,
            task_id=result["task_id"],
            attachments=attachments,
            result_ref=payload.get("result_ref"),
        )
        state = "submitting" if voice else "delivered"
        with self.db:
            self.db.execute(
                "INSERT INTO chat_result_receipts VALUES(?,?,?,?)", (ident, context["session"], state, ident)
            )
        if voice:
            try:
                await c.speech_feedback(
                    context["turn"],
                    (receipt["epoch"], receipt["revision"]),
                    "result",
                    spoken_result(payload["body"], "completed"),
                    delivery_id=ident,
                )
                state = "submitted"
            except Exception:
                state = "unknown"
            with self.db:
                self.db.execute("UPDATE chat_result_receipts SET state=? WHERE id=?", (state, ident))
        return {"state": state, "message_id": ident}
