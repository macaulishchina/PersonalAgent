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
            "CREATE TABLE IF NOT EXISTS conversation_inputs(id TEXT PRIMARY KEY, session TEXT NOT NULL, source TEXT NOT NULL, mode TEXT NOT NULL, target TEXT, question TEXT, work TEXT NOT NULL, state TEXT NOT NULL, fingerprint TEXT, result TEXT)"
        )
        self.db.commit()

    def get(self, ident, session):
        row = self.db.execute(
            "SELECT id,session,source,mode,target,question,work,state,fingerprint,result FROM conversation_inputs WHERE id=? AND session=?",
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
                ),
                row,
            )
        )

    async def request(self, request):
        if current_usage.get() is not None:
            raise PermissionError("Entrance input management is not an execution tool")
        args = request.arguments
        if set(args) - {"session_id", "request_id", "source", "mode", "text"}:
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
                    return {"state": old["state"], "work_id": old["work"]}
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
                        "INSERT INTO conversation_inputs VALUES(?,?,?,?,?,?,?,'prepared',NULL,NULL)",
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
                                "UPDATE conversation_inputs SET state='accepted',result=? WHERE id=?",
                                (json.dumps(receipt), ident),
                            )
                        old = self.get(ident, session)
                return {
                    "state": old["state"],
                    "result": json.loads(old["result"]) if old["result"] else None,
                    "work_id": old["work"],
                }
            if request.action == "cancel":
                if old["state"] == "prepared":
                    with self.db:
                        self.db.execute(
                            "UPDATE conversation_inputs SET state='cancelled' WHERE id=?", (ident,)
                        )
                return {"state": self.get(ident, session)["state"]}
            text = args.get("text")
            if (
                request.action != "commit"
                or not isinstance(text, str)
                or not text.strip()
                or len(text) > 16000
            ):
                raise ValueError("Invalid final transcript")
            fingerprint = hashlib.sha256(text.encode()).hexdigest()
            if old["fingerprint"] and old["fingerprint"] != fingerprint:
                raise ValueError("Input identity reused with different text")
            if old["state"] != "prepared":
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
                    "UPDATE conversation_inputs SET state='accepted',result=? WHERE id=?",
                    (json.dumps(result), ident),
                )
            return {"state": "accepted", "result": result}
