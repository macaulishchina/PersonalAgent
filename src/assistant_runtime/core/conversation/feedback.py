"""Durable, once-only output submission receipts, never a claim that speech was heard."""

import re
import time


class Feedback:
    def __init__(self, db):
        self.db = db
        db.execute("""CREATE TABLE IF NOT EXISTS chat_feedback (
            turn_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, voice_epoch INTEGER NOT NULL,
            revision INTEGER NOT NULL, source TEXT NOT NULL, state TEXT NOT NULL, updated REAL NOT NULL)
        """)
        if "ack_state" not in {row[1] for row in db.execute("PRAGMA table_info(chat_feedback)")}:
            db.execute("ALTER TABLE chat_feedback ADD COLUMN ack_state TEXT NOT NULL DEFAULT 'pending'")
        db.execute("""CREATE TABLE IF NOT EXISTS chat_feedback_inputs (
            message_id TEXT PRIMARY KEY, turn_id TEXT NOT NULL)
        """)

    def recover(self, session):
        with self.db:
            self.db.execute(
                "UPDATE chat_feedback SET state=CASE WHEN state='dispatching' THEN 'unknown' ELSE 'closed' END, updated=? "
                "WHERE session_id=? AND state IN ('bound','dispatching')",
                (time.time(), session),
            )
            self.db.execute(
                "UPDATE chat_feedback SET ack_state='unknown' WHERE session_id=? AND ack_state='dispatching'",
                (session,),
            )

    def bind(self, turn, session, epoch, revision, source):
        if self.get(turn):
            return
        with self.db:
            self.db.execute(
                "UPDATE chat_feedback SET state='superseded',updated=? WHERE session_id=? AND state='bound'",
                (time.time(), session),
            )
            self.db.execute(
                "INSERT OR IGNORE INTO chat_feedback "
                "(turn_id,session_id,voice_epoch,revision,source,state,updated) VALUES(?,?,?,?,?,'bound',?)",
                (turn, session, epoch, revision, source, time.time()),
            )

    def accept_input(self, message, turn, session, epoch, revision, source):
        """Renew output ownership only at a live provider-accepted input boundary."""
        with self.db:
            added = self.db.execute(
                "INSERT OR IGNORE INTO chat_feedback_inputs VALUES(?,?)", (message, turn)
            ).rowcount
            if added:
                renewed = self.db.execute(
                    "UPDATE chat_feedback SET voice_epoch=?,revision=?,source=?,state='bound',"
                    "ack_state='pending',updated=? WHERE turn_id=? AND session_id=? "
                    "AND (voice_epoch!=? OR revision!=? OR source!=?)",
                    (epoch, revision, source, time.time(), turn, session, epoch, revision, source),
                )
                return renewed.rowcount > 0
            return False

    def get(self, turn):
        row = self.db.execute(
            "SELECT voice_epoch,revision,source,state,ack_state FROM chat_feedback WHERE turn_id=?", (turn,)
        ).fetchone()
        return dict(zip(("epoch", "revision", "source", "state", "ack_state"), row)) if row else None

    def acknowledge(self, turn, epoch, revision, state):
        with self.db:
            return (
                self.db.execute(
                    "UPDATE chat_feedback SET ack_state=?,updated=? WHERE turn_id=? "
                    "AND voice_epoch=? AND revision=? AND ack_state=?",
                    (
                        state,
                        time.time(),
                        turn,
                        epoch,
                        revision,
                        "pending" if state == "dispatching" else "dispatching",
                    ),
                ).rowcount
                > 0
            )

    def finish(self, turn, state):
        with self.db:
            self.db.execute(
                "UPDATE chat_feedback SET state=?,updated=? WHERE turn_id=? AND state IN ('bound','dispatching')",
                (state, time.time(), turn),
            )

    def close(self, session, epoch):
        with self.db:
            self.db.execute(
                "UPDATE chat_feedback SET state='closed',updated=? WHERE session_id=? AND voice_epoch=? AND state IN ('bound','dispatching')",
                (time.time(), session, epoch),
            )


def spoken_result(text, status):
    if status == "interrupted":
        return ""
    if not text.strip():
        return "本次执行失败，详细原因请查看执行记录。" if status == "failed" else ""
    # Tool output/error blocks are facts. Removing them leaves an ambiguous
    # "command output:" and invites the renderer to invent the missing result.
    text = re.sub(r"```(?:[a-zA-Z0-9_+-]+\n)?(.*?)```", lambda m: m.group(1).strip(), text, flags=re.S)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"[*`#]", "", text).strip()
    if status != "completed":
        text = "本次执行未完成。" + text
    if len(text) > 1000:
        text = "执行结果已保存在对话记录中，内容较长，我先不逐项朗读。"
    return text
