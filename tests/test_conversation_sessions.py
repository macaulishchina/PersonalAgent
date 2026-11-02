"""Stable conversation ownership and queued input; no simulated provider claims."""

import asyncio
import json
from pathlib import Path
import tempfile
import sqlite3
from types import SimpleNamespace
import unittest
import uuid

from assistant_runtime.api import ConversationRequest
from assistant_runtime.api.conversation import current_conversation
from assistant_runtime.api.contracts import ConversationFeedbackRequest
from assistant_runtime.app.config import Config
from assistant_runtime.app.store import Store
from assistant_runtime.core.conversation.plugin import Conversations
from assistant_runtime.core.conversation.sessions import Sessions
from assistant_runtime.core.conversation.store import ConversationStore
from assistant_runtime.core.cli.client import Chat


class Context:
    def __init__(self):
        self.tasks = []

    def register(self, *args, **kwargs):
        pass

    def spawn(self, coro, label):
        task = asyncio.create_task(coro)
        self.tasks.append(task)
        return task

    async def close(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)


class Backend:
    def __init__(self, ident=None):
        self.session_id = ident
        self.execution_session_id = ident
        self.active_turn = self.last_turn = None
        self.calls = []
        self.client = SimpleNamespace(settings=SimpleNamespace(enabled=True), started=True, workers={})

    @property
    def busy(self):
        return self.active_turn is not None

    def restored_conversation_events(self):
        return []

    async def open_conversation(self, ident):
        return Backend(ident)

    async def submit(self, text, images=(), *, cancelled=lambda: False):
        if cancelled():
            raise ValueError("Cancelled")
        assert not self.busy, "input must not steer a busy conversation"
        self.calls.append(text)
        self.active_turn = self.last_turn = uuid.uuid4().hex
        self.on_conversation({"kind": "turn_started", "turn_id": self.active_turn})

    async def finish(self, status="completed"):
        turn = self.active_turn
        self.on_conversation({"kind": "turn_completed", "turn_id": turn, "status": status})
        self.active_turn = None
        await self.on_conversation_done(turn, "result", status)

    async def cancel(self, expected=None):
        if not self.busy or expected and expected != self.active_turn:
            return False
        await self.finish("interrupted")
        return True

    async def conversation_input(self, action, text, turn, question_id=None):
        self.calls.append((action, text, turn, question_id))
        return {"state": action}


class SessionsTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "state.db")
        self.host = SimpleNamespace(
            store=self.store,
            config=Config(root=self.root, plugins=[]),
            backend=Backend(),
            assistant=None,
            memory=None,
        )
        self.registry = Conversations(self.host)
        self.context = Context()
        await self.registry.start(self.context)

    async def asyncTearDown(self):
        await self.context.close()
        self.store.db.close()
        self.temp.cleanup()

    async def call(self, action, **args):
        return await self.registry.request(ConversationRequest(action, args))

    async def send(self, session, text, ident=None):
        ident = ident or str(uuid.uuid4())
        return ident, await self.call("submit", session_id=session, request_id=ident, text=text)

    async def started(self, session, count=1):
        async with asyncio.timeout(2):
            while len((await self.registry.get(session)).backend.calls) < count:
                await asyncio.sleep(0.005)
        return await self.registry.get(session)

    async def test_cli_binding_is_independent_of_web_and_other_client_views(self):
        web = self.registry.main.session
        a = await self.call("open", operator="operator")
        self.assertNotEqual(a["id"], web)
        client = Chat(
            lambda action, args: self.registry.request(ConversationRequest(action, args)),
            output=lambda *a: None,
            operator="operator",
        )
        await client.select()
        b = await self.call("create", title="second")
        await self.call("open", operator="operator", session_id=b["id"])
        self.assertEqual(client.session, a["id"])
        self.assertEqual(self.registry.sessions.bound("web"), web)
        self.assertEqual((await self.call("open", operator="operator"))["id"], b["id"])

    async def test_ordered_turns_cross_session_parallel_and_duplicate_conflicts(self):
        a, b = [(await self.call("create"))["id"] for _ in range(2)]
        ident, first = await self.send(a, "A1")
        self.assertEqual((await self.send(a, "A1", ident))[1], first)
        with self.assertRaises(ValueError):
            await self.send(b, "A1", ident)
        ca = await self.started(a)
        await self.send(a, "A2")
        await self.send(b, "B1")
        cb = await self.started(b)
        self.assertEqual(ca.backend.calls, ["A1"])
        self.assertTrue(cb.backend.busy)
        await ca.backend.finish()
        await self.started(a, 2)
        self.assertEqual(ca.backend.calls, ["A1", "A2"])
        self.assertEqual(
            [
                m["text"]
                for m in (await self.call("history", session_id=b))["messages"]
                if m["role"] == "user"
            ],
            ["B1"],
        )

    async def test_queued_cancel_and_active_request_checks(self):
        session = (await self.call("create"))["id"]
        first, _ = await self.send(session, "first")
        chat = await self.started(session)
        second, _ = await self.send(session, "second")
        await self.call("cancel", session_id=session, request_id=second)
        with self.assertRaises(ValueError):
            await self.call("supplement", session_id=session, request_id=second, text="wrong")
        await self.call("supplement", session_id=session, request_id=first, text="extra")
        self.assertEqual(chat.backend.calls[1][0], "supplement")
        await self.call("cancel", session_id=session)
        self.assertFalse(chat.backend.busy)
        self.assertEqual((await self.call("queue", session_id=session))["requests"], [])

    async def test_edit_queued_preserves_identity_and_initial_submission_deduplication(self):
        session = (await self.call("create"))["id"]
        await self.send(session, "running")
        chat = await self.started(session)
        ident, sent = await self.send(session, "original")
        item = next(r for r in chat.queue() if r["id"] == ident)
        updated = await self.call(
            "edit_queued", session_id=session, request_id=ident, revision=item["revision"], text="edited"
        )
        self.assertEqual(updated["message"]["id"], sent["message"]["id"])
        self.assertEqual(updated["message"]["ordinal"], sent["message"]["ordinal"])
        self.assertEqual(updated["message"]["text"], "edited")
        self.assertEqual(updated["message"]["status"], "queued")
        self.assertEqual((await self.send(session, "original", ident))[1]["message"]["text"], "edited")
        with self.assertRaises(ValueError):
            await self.send(session, "edited", ident)
        with self.assertRaises(ValueError):
            await self.call(
                "edit_queued", session_id=session, request_id=ident, revision=item["revision"], text="stale"
            )
        current = next(r for r in chat.queue() if r["id"] == ident)
        with self.assertRaises(ValueError):
            await self.call(
                "edit_queued", session_id=session, request_id=ident, revision=current["revision"], text=" "
            )
        self.assertEqual(next(r for r in chat.queue() if r["id"] == ident), current)
        self.assertEqual(chat.backend.calls, ["running"])

    async def test_waiting_queue_dispatches_latest_order_and_edited_payload(self):
        session = (await self.call("create"))["id"]
        chat = await self.registry.get(session)
        chat.backend.active_turn = chat.backend.last_turn = "external-turn"
        first, _ = await self.send(session, "first")
        second, _ = await self.send(session, "second")
        await asyncio.sleep(0)  # The drain is waiting for the already active provider turn.
        item = next(r for r in chat.queue() if r["id"] == second)
        await self.call(
            "edit_queued",
            session_id=session,
            request_id=second,
            revision=item["revision"],
            text="second edited",
        )
        await self.call(
            "reorder_queue",
            session_id=session,
            request_ids=[second, first],
            expected_order=[first, second],
        )
        await chat.backend.finish()
        await self.started(session)
        self.assertEqual(chat.backend.calls, ["second edited"])
        await chat.backend.finish()
        await self.started(session, 2)
        self.assertEqual(chat.backend.calls, ["second edited", "first"])

    async def test_history_follows_dispatch_order_not_original_queue_submission_time(self):
        session = (await self.call("create"))["id"]
        await self.send(session, "active")
        chat = await self.started(session)
        first, _ = await self.send(session, "first queued")
        second, _ = await self.send(session, "second queued")
        chat.store.put(
            "late-reply", session, role="assistant", text="active reply finished", status="complete"
        )
        await self.call(
            "reorder_queue", session_id=session, request_ids=[second, first], expected_order=[first, second]
        )
        await chat.backend.finish()
        await self.started(session, 2)
        await chat.backend.finish()
        await self.started(session, 3)
        history = (await self.call("history", session_id=session))["messages"]
        self.assertEqual(
            [m["text"] for m in history],
            ["active", "active reply finished", "second queued", "first queued"],
        )
        self.assertEqual(
            [m["id"] for m in history if m["role"] == "user"][-2:], ["cli:" + second, "cli:" + first]
        )

    async def test_reordering_is_session_scoped_and_rejects_stale_or_partial_orders(self):
        a, b = [(await self.call("create"))["id"] for _ in range(2)]
        active, _ = await self.send(a, "A active")
        chat = await self.started(a)
        first, _ = await self.send(a, "A first")
        await self.send(b, "B active")
        await self.started(b)
        foreign, _ = await self.send(b, "B queued")
        second, _ = await self.send(a, "A second")
        before = dict(self.store.db.execute("SELECT id,ordinal FROM conversation_queue"))
        await self.call(
            "reorder_queue", session_id=a, request_ids=[second, first], expected_order=[first, second]
        )
        after = dict(self.store.db.execute("SELECT id,ordinal FROM conversation_queue"))
        self.assertEqual(after[foreign], before[foreign])
        self.assertEqual(after[active], before[active])
        self.assertEqual(after[first], before[second])
        self.assertEqual(after[second], before[first])
        for ids, expected in [
            ([first, second], [first, second]),
            ([second], [second, first]),
            ([second, second], [second, first]),
            ([second, foreign], [second, first]),
            ([active, first], [second, first]),
        ]:
            with self.assertRaises(ValueError):
                await self.call("reorder_queue", session_id=a, request_ids=ids, expected_order=expected)
            self.assertEqual(dict(self.store.db.execute("SELECT id,ordinal FROM conversation_queue")), after)
        self.assertTrue(chat.backend.busy)

    async def test_remove_queued_never_cancels_an_active_or_foreign_request(self):
        session = (await self.call("create"))["id"]
        active, _ = await self.send(session, "running")
        chat = await self.started(session)
        queued, sent = await self.send(session, "remove me")
        item = next(r for r in chat.queue() if r["id"] == queued)
        foreign = (await self.call("create"))["id"]
        with self.assertRaises(ValueError):
            await self.call("remove_queued", session_id=foreign, request_id=queued, revision=item["revision"])
        result = await self.call(
            "remove_queued", session_id=session, request_id=queued, revision=item["revision"]
        )
        self.assertEqual(result["message"]["id"], sent["message"]["id"])
        self.assertEqual(result["message"]["status"], "cancelled")
        self.assertNotIn(queued, [r["id"] for r in result["requests"]])
        running = next(r for r in chat.queue() if r["id"] == active)
        for action in ["remove_queued", "edit_queued"]:
            with self.assertRaises(ValueError):
                await self.call(
                    action,
                    session_id=session,
                    request_id=active,
                    revision=running["revision"],
                    **({"text": "wrong"} if action == "edit_queued" else {}),
                )
        self.assertTrue(chat.backend.busy)
        self.assertEqual(chat.backend.calls, ["running"])

    async def test_queue_edit_preserves_accepted_media_and_notification_context(self):
        session = (await self.call("create"))["id"]
        await self.send(session, "running")
        chat = await self.started(session)
        ident, sent = await self.send(session, "text")
        raw = self.store.db.execute("SELECT payload FROM conversation_queue WHERE id=?", (ident,)).fetchone()[
            0
        ]
        payload = json.loads(raw)
        payload["media"] = [{"id": "accepted-image", "name": "image.jpg", "type": "image/jpeg"}]
        payload["notification_context"] = "accepted notification reference"
        with self.store.db:
            self.store.db.execute(
                "UPDATE conversation_queue SET payload=? WHERE id=?", (json.dumps(payload), ident)
            )
        item = next(r for r in chat.queue() if r["id"] == ident)
        result = await self.call(
            "edit_queued", session_id=session, request_id=ident, revision=item["revision"], text="new text"
        )
        saved = json.loads(
            self.store.db.execute("SELECT payload FROM conversation_queue WHERE id=?", (ident,)).fetchone()[0]
        )
        self.assertEqual(saved["media"], payload["media"])
        self.assertEqual(saved["notification_context"], payload["notification_context"])
        self.assertEqual(result["message"]["attachments"], payload["media"])
        self.assertEqual(result["message"]["id"], sent["message"]["id"])

    async def test_reorder_failure_rolls_back_all_ordinal_changes(self):
        session = (await self.call("create"))["id"]
        await self.send(session, "running")
        await self.started(session)
        first, _ = await self.send(session, "first")
        second, _ = await self.send(session, "second")
        before = list(self.store.db.execute("SELECT id,ordinal FROM conversation_queue ORDER BY ordinal"))
        self.store.db.execute(
            f"CREATE TEMP TRIGGER fail_reorder BEFORE UPDATE OF ordinal ON conversation_queue "
            f"WHEN NEW.id='{second}' AND NEW.ordinal>0 BEGIN SELECT RAISE(ABORT,'test rollback'); END"
        )
        with self.assertRaises(sqlite3.DatabaseError):
            await self.call(
                "reorder_queue",
                session_id=session,
                request_ids=[second, first],
                expected_order=[first, second],
            )
        self.assertEqual(
            list(self.store.db.execute("SELECT id,ordinal FROM conversation_queue ORDER BY ordinal")), before
        )

    async def test_queued_mutations_reject_a_request_already_being_prepared(self):
        session = (await self.call("create"))["id"]
        chat = await self.registry.get(session)
        entered, release = asyncio.Event(), asyncio.Event()

        async def prepare(text):
            entered.set()
            await release.wait()

        chat.prepare_dispatch = prepare
        ident, _ = await self.send(session, "original")
        item = next(r for r in chat.queue() if r["id"] == ident)
        await entered.wait()
        for action in ["remove_queued", "edit_queued"]:
            with self.assertRaises(ValueError):
                await self.call(
                    action,
                    session_id=session,
                    request_id=ident,
                    revision=item["revision"],
                    **({"text": "do not replace"} if action == "edit_queued" else {}),
                )
        self.assertNotIn(ident, chat.cancel_pending)
        release.set()
        await self.started(session)
        self.assertEqual(chat.backend.calls, ["original"])

    async def test_cancel_during_preparation_cannot_dispatch_or_cancel_next_turn(self):
        session = (await self.call("create"))["id"]
        chat = await self.registry.get(session)
        entered, release = asyncio.Event(), asyncio.Event()

        async def prepare(text):
            entered.set()
            await release.wait()

        chat.prepare_dispatch = prepare
        first, _ = await self.send(session, "cancel before model")
        await entered.wait()
        await self.call("cancel", session_id=session, request_id=first)
        await self.send(session, "next")
        release.set()
        await self.started(session)
        self.assertEqual(chat.backend.calls, ["next"])
        self.assertEqual(chat.store.get("cli:" + first)["status"], "cancelled")
        self.assertFalse((await self.call("cancel", session_id=session, request_id=first))["cancelled"])
        self.assertTrue(chat.backend.busy)

    async def test_legacy_attachment_cannot_be_read_or_submitted_from_another_session(self):
        from assistant_runtime.core.conversation.access import ConversationAccess

        main = self.registry.main
        ident = "a" * 32
        main.store.put("legacy-image", main.session, attachments=[{"id": ident}])
        other = await self.registry.get((await self.call("create"))["id"])
        with self.assertRaises(FileNotFoundError):
            ConversationAccess(lambda: other).attachment(ident)
        with self.assertRaisesRegex(ValueError, "another conversation"):
            await other.submit(str(uuid.uuid4()), "wrong image", [ident])

    async def test_queue_bound_and_recovery_never_replay(self):
        self.host.config.execution = {"queue_size": 1}
        session = (await self.call("create"))["id"]
        await self.send(session, "running")
        await self.started(session)
        await self.send(session, "waiting")
        with self.assertRaisesRegex(ValueError, "queue is full"):
            await self.send(session, "overflow")
        await self.context.close()
        self.host.backend = Backend()
        self.registry = Conversations(self.host)
        self.context = Context()
        await self.registry.start(self.context)
        self.assertEqual((await self.call("queue", session_id=session))["requests"], [])
        states = [
            r[0] for r in self.store.db.execute("SELECT state FROM conversation_queue ORDER BY ordinal")
        ]
        self.assertEqual(states, ["unknown", "interrupted"])
        self.assertNotIn(session, self.registry.opened)

    async def test_task_result_stays_in_original_session_after_turn_and_switch(self):
        a, b = [(await self.call("create"))["id"] for _ in range(2)]
        await self.send(a, "delegate")
        chat = await self.started(a)
        token = current_conversation.set(a)
        try:
            binding = await self.registry.feedback(ConversationFeedbackRequest("capture"))
        finally:
            current_conversation.reset(token)
        await chat.backend.finish()
        await self.call("open", operator="operator", session_id=b)
        result = {"id": "result-one", "task_id": "task-one", "payload": {"title": "Done", "body": "evidence"}}
        request = ConversationFeedbackRequest("deliver", context=binding, result=result)
        self.assertEqual((await self.registry.feedback(request))["state"], "delivered")
        self.assertEqual((await self.registry.feedback(request))["state"], "delivered")
        self.assertEqual(self.registry.main.store.get("result-one")["session_id"], a)
        self.assertEqual((await self.call("history", session_id=b))["messages"], [])

    async def test_cli_exit_keeps_execution_and_invalid_switch_keeps_selection(self):
        client = Chat(
            lambda action, args: self.registry.request(ConversationRequest(action, args)),
            output=lambda *a: None,
        )
        await client.select()
        session = client.session
        await client.command("a task")
        chat = await self.started(session)
        with self.assertRaises(ValueError):
            await client.command("/switch unknown")
        self.assertEqual(client.session, session)
        self.assertFalse(await client.command("/exit"))
        self.assertTrue(chat.backend.busy)

    async def test_cli_cancelled_input_and_eof_only_disconnect(self):
        output = []
        client = Chat(
            lambda action, args: self.registry.request(ConversationRequest(action, args)),
            output=output.append,
        )
        values = iter([asyncio.CancelledError(), "continue after ctrl-c", EOFError()])

        async def read():
            value = next(values)
            if isinstance(value, BaseException):
                raise value
            return value

        await client.run(read=read)
        chat = await self.started(client.session)
        self.assertTrue(chat.backend.busy)
        self.assertTrue(any("Input cleared" in line for line in output))
        self.assertTrue(any("Disconnected" in line for line in output))


class MigrationTest(unittest.TestCase):
    def test_conversion_failure_rolls_back_and_backup_can_be_reopened(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder) / "state.db")
            try:
                store.set("session_id", "retained")
                store.db.set_authorizer(
                    lambda action, table, *args: sqlite3.SQLITE_DENY
                    if action == sqlite3.SQLITE_INSERT and table == "conversation_bindings"
                    else sqlite3.SQLITE_OK
                )
                with self.assertRaises(sqlite3.DatabaseError):
                    Sessions(store)
                store.db.set_authorizer(None)
                self.assertIsNone(
                    store.db.execute(
                        "SELECT name FROM sqlite_master WHERE name='conversation_sessions'"
                    ).fetchone()
                )
                self.assertEqual(store.get("session_id"), "retained")
                self.assertEqual(Sessions(store).bound("web"), "retained")
            finally:
                store.db.close()

    def test_backup_preserves_legacy_identity_and_conversion_is_idempotent(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder) / "state.db")
            try:
                chat = ConversationStore(store)
                chat.put("old-message", "old-session", text="preserved", status="complete")
                store.db.execute(
                    "CREATE TABLE chat_result_receipts (id TEXT PRIMARY KEY, session TEXT NOT NULL, "
                    "state TEXT NOT NULL, message_id TEXT NOT NULL)"
                )
                store.db.execute(
                    "INSERT INTO chat_result_receipts VALUES('receipt','report-session','delivered','report')"
                )
                store.set("session_id", "old-session")
                sessions = Sessions(store)
                self.assertEqual(sessions.bound("web"), "old-session")
                self.assertEqual(sessions.get("report-session")["id"], "report-session")
                self.assertEqual(
                    store.db.execute("SELECT state FROM chat_result_receipts WHERE id='receipt'").fetchone(),
                    ("delivered",),
                )
                self.assertEqual(Sessions(store).get("old-session")["id"], "old-session")
                self.assertEqual(len(list(Path(folder).glob("conversation-before-migration-*.sqlite3"))), 1)
                self.assertEqual(chat.get("old-message")["text"], "preserved")
            finally:
                store.db.close()

    def test_ambiguous_main_refuses_without_partial_tables(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder) / "state.db")
            try:
                chat = ConversationStore(store)
                chat.put("a", "a", text="a")
                chat.put("b", "b", text="b")
                with self.assertRaisesRegex(ValueError, "Ambiguous"):
                    Sessions(store)
                self.assertIsNone(
                    store.db.execute(
                        "SELECT name FROM sqlite_master WHERE name='conversation_sessions'"
                    ).fetchone()
                )
            finally:
                store.db.close()
