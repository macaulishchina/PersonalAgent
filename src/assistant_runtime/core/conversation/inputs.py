"""Reserve target before recording; acknowledge or reconcile, never blindly replay steering."""

import asyncio
import hashlib
import json
import re
import uuid

from assistant_runtime.api.ai_usage import UsageScope, current_usage
from assistant_runtime.api.conversation import ConversationRequest, ConversationInputChanged


class EntranceInputs:
    def __init__(self, conversations):
        self.owner = conversations
        self.db = conversations.sessions.db
        self.lock = asyncio.Lock()
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS conversation_inputs(id TEXT PRIMARY KEY, session TEXT NOT NULL, source TEXT NOT NULL, mode TEXT NOT NULL, target TEXT, question TEXT, work TEXT NOT NULL, state TEXT NOT NULL, fingerprint TEXT, result TEXT, draft TEXT)"
        )
        if "draft" not in {row[1] for row in self.db.execute("PRAGMA table_info(conversation_inputs)")}:
            self.db.execute("ALTER TABLE conversation_inputs ADD COLUMN draft TEXT")
        self.db.commit()

    def get(self, ident, session):
        row = self.db.execute(
            "SELECT id,session,source,mode,target,question,work,state,fingerprint,result,draft FROM conversation_inputs WHERE id=? AND session=?",
            (ident, session),
        ).fetchone()
        if row is None:
            return None
        return dict(
            zip(
                (
                    "id",
                    "session",
                    "source",
                    "mode",
                    "target",
                    "question",
                    "work",
                    "state",
                    "fingerprint",
                    "result",
                    "draft",
                ),
                row,
            )
        )

    async def request(self, request):
        if current_usage.get() is not None:
            raise PermissionError("Entrance input management is not an execution tool")
        args = request.arguments
        if set(args) - {"session_id", "request_id", "source", "mode", "text", "offset", "limit"}:
            raise ValueError("Unknown entrance input field")
        ident = str(uuid.UUID(args.get("request_id")))
        session = args.get("session_id")
        self.owner.sessions.get(session)
        async with self.lock:
            old = self.get(ident, session)
            if request.action == "prepare":
                source, mode = args.get("source"), args.get("mode", "steer")
                if (
                    not isinstance(source, str)
                    or not re.fullmatch(r"terminal:[a-zA-Z0-9_-]{1,64}", source)
                    or mode not in {"steer", "queue"}
                ):
                    raise ValueError("Invalid entrance input")
                if old:
                    if (source, mode) != (old["source"], old["mode"]):
                        raise ValueError("Input identity conflict")
                    result = {"state": old["state"], "work_id": old["work"]}
                    if old["state"] == "review":
                        result["text_length"] = len(old["draft"] or "")
                    return result
                chat = await self.owner.get(session)
                state = chat.state()
                if state.get("configuration_required") or not getattr(
                    chat.backend.client.settings, "enabled", True
                ):
                    raise ValueError("Model configuration required before recording")
                target = (
                    state.get("active_request_id") if mode == "steer" or state.get("question_id") else None
                )
                work = (state.get("ai_usage") or {}).get("work_id") if target else None
                if target and not work:
                    bound = await self.owner.host.ai_usage.service.binding(target)
                    if bound is None:
                        raise ValueError("Active execution identity not ready; try after acceptance")
                    work = bound.work_id
                work = work or ident
                usage = self.owner.host.ai_usage.service
                await usage.bind(ident, UsageScope(work, source))
                with self.db:
                    self.db.execute(
                        "INSERT INTO conversation_inputs(id,session,source,mode,target,question,work,state,fingerprint,result) VALUES(?,?,?,?,?,?,?,'prepared',NULL,NULL)",
                        (ident, session, source, mode, target, state.get("question_id"), work),
                    )
                return {"state": "prepared", "work_id": work}
            if old is None:
                if request.action == "receipt":
                    return {"state": "absent"}
                raise ValueError("Prepare input before submitting")
            if request.action == "receipt":
                if old["state"] == "unknown":
                    receipt = await self.owner.request(
                        ConversationRequest("receipt", {"session_id": session, "request_id": ident})
                    )
                    if receipt["found"]:
                        with self.db:
                            self.db.execute(
                                "UPDATE conversation_inputs SET state='accepted',result=?,draft=NULL WHERE id=?",
                                (json.dumps(receipt), ident),
                            )
                        old = self.get(ident, session)
                return self.receipt(old)
            if request.action == "transcript":
                offset, limit = args.get("offset", 0), args.get("limit", 80)
                text = old["draft"]
                if (
                    old["state"] != "review"
                    or not isinstance(text, str)
                    or type(offset) is not int
                    or not 0 <= offset <= len(text)
                    or type(limit) is not int
                    or not 1 <= limit <= 240
                    or set(args) - {"session_id", "request_id", "offset", "limit"}
                ):
                    raise ValueError("No matching transcript page awaiting confirmation")
                end = min(len(text), offset + limit)
                return {
                    "state": "review",
                    "text": text[offset:end],
                    "offset": offset,
                    "next_offset": end if end < len(text) else None,
                    "total": len(text),
                }
            if request.action == "cancel":
                if old["state"] in {"prepared", "review", "target_changed", "question_changed"}:
                    with self.db:
                        self.db.execute(
                            "UPDATE conversation_inputs SET state='cancelled',draft=NULL WHERE id=?", (ident,)
                        )
                return {"state": self.get(ident, session)["state"]}
            confirming = request.action == "confirm"
            if confirming:
                if set(args) - {"session_id", "request_id"}:
                    raise ValueError("Confirmation uses the saved transcript, without replacement text")
                if old["state"] in {"accepted", "unknown"}:
                    return {
                        "state": old["state"],
                        "result": json.loads(old["result"]) if old["result"] else None,
                    }
                if old["state"] != "review":
                    raise ValueError("No input awaiting explicit confirmation")
            text = old["draft"] if confirming else args.get("text")
            if (
                request.action not in {"commit", "review", "confirm"}
                or not isinstance(text, str)
                or not text.strip()
                or len(text) > 16000
            ):
                raise ValueError("Invalid final transcript")
            fingerprint = hashlib.sha256(text.encode()).hexdigest()
            if old["fingerprint"] and old["fingerprint"] != fingerprint:
                raise ValueError("Input identity reused with different text")
            if request.action == "review":
                if old["state"] == "prepared":
                    with self.db:
                        self.db.execute(
                            "UPDATE conversation_inputs SET state='review',fingerprint=?,draft=? WHERE id=?",
                            (fingerprint, text, ident),
                        )
                return self.receipt(self.get(ident, session))
            if old["state"] == "review" and not confirming:
                raise ValueError("Input awaits explicit confirmation")
            if old["state"] not in {"prepared", "review"}:
                return {"state": old["state"], "result": json.loads(old["result"]) if old["result"] else None}
            chat = await self.owner.get(session)
            state = chat.state()
            target = old["target"]
            changed = (
                "target_changed"
                if target and state.get("active_request_id") not in {None, target}
                else "question_changed"
                if old["question"] and state.get("question_id") != old["question"]
                else None
            )
            if changed:
                with self.db:
                    self.db.execute(
                        "UPDATE conversation_inputs SET state=?,fingerprint=? WHERE id=?",
                        (changed, fingerprint, ident),
                    )
                return {"state": changed, "action": "review_current_input"}
            # Mark uncertain durably before handing a supplement to an external executor.
            with self.db:
                self.db.execute(
                    "UPDATE conversation_inputs SET state='unknown',fingerprint=? WHERE id=?",
                    (fingerprint, ident),
                )
            try:
                if target and state.get("active_request_id") == target:
                    result = await self.owner.active_input(
                        chat,
                        "answer" if old["question"] else "supplement",
                        {
                            "request_id": target,
                            "text": text,
                            **({"question_id": old["question"]} if old["question"] else {}),
                        },
                        source=old["source"],
                    )
                else:
                    result = await chat.submit(ident, text, [], source=old["source"])
            except ConversationInputChanged:
                # Owner confirms nothing was dispatched. A finished target becomes a new queued input.
                state = chat.state()
                if old["question"] or state.get("active_request_id"):
                    with self.db:
                        self.db.execute(
                            "UPDATE conversation_inputs SET state='target_changed' WHERE id=?", (ident,)
                        )
                    return {"state": "target_changed"}
                result = await chat.submit(ident, text, [], source=old["source"])
            with self.db:
                self.db.execute(
                    "UPDATE conversation_inputs SET state='accepted',result=?,draft=NULL WHERE id=?",
                    (json.dumps(result), ident),
                )
            return {"state": "accepted", "result": result}

    @staticmethod
    def receipt(item):
        result = {
            "state": item["state"],
            "result": json.loads(item["result"]) if item["result"] else None,
            "work_id": item["work"],
        }
        if item["state"] == "review":
            result["text_length"] = len(item["draft"] or "")
        return result
