"""Public entrance view; conversation owns message and attachment storage."""

import re
import time


class ConversationAccess:
    def __init__(self, conversation, project=lambda messages: None):
        self.__get, self.__project = conversation, project

    @property
    def __conversation(self):
        return self.__get()

    def state(self):
        return self.__conversation.state()

    async def submit(self, *args):
        return await self.__conversation.submit(*args)

    async def control(self, action):
        return await self.__conversation.control(action)

    def project(self, messages):
        self.__project(messages)

    def history(self, before):
        conversation = self.__conversation
        messages = conversation.store.history(conversation.session, before)
        self.project(messages)
        return {
            "messages": messages,
            "cursor": conversation.store.cursor(),
            "state": self.state(),
            "before": messages[0]["ordinal"] if len(messages) == 60 else None,
        }

    def changes(self, cursor):
        conversation = self.__conversation
        events = conversation.store.changes(conversation.session, cursor)
        if events:
            self.project([e["data"] for e in events if e["kind"] == "message" and e["data"]])
        return events

    async def upload(self, data, name):
        conversation = self.__conversation
        reference = await conversation.media.add_image(data, name)
        with conversation.store.db:
            conversation.store.db.execute(
                "INSERT INTO conversation_uploads VALUES(?,?)", (reference["id"], conversation.session)
            )
        return reference

    def attachment(self, ident):
        if not re.fullmatch(r"[a-f0-9]{32}", ident):
            raise FileNotFoundError()
        conversation = self.__conversation
        if conversation.session not in conversation.store.attachment_sessions(ident):
            raise FileNotFoundError()
        media = conversation.media
        row = media.db.execute("SELECT media_type FROM chat_uploads WHERE id=?", (ident,)).fetchone()
        path = media.folder / ident
        if not row or row[0] not in {"image/jpeg", "video/mp4"} or not path.is_file():
            raise FileNotFoundError()
        return path, row[0]

    def expire_uploads(self):
        media = self.__conversation.media
        for (ident,) in media.db.execute(
            "SELECT id FROM chat_uploads WHERE attached=0 AND created<?", (time.time() - 86400,)
        ).fetchall():
            (media.folder / ident).unlink(missing_ok=True)
            with media.db:
                media.db.execute("DELETE FROM chat_uploads WHERE id=? AND attached=0", (ident,))
