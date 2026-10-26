"""Receipt evidence and replay windows; synthetic model tests live in execution_service."""

import sqlite3
import unittest
import uuid
from types import SimpleNamespace

from assistant_runtime.api.runs import ExecutionRequest
from assistant_runtime.core.execution.records import RunRecords
from assistant_runtime.core.pi.input_receipts import consumed, track
import test_execution_service as support


class MessageTest(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = support.ExecutionServiceTest.asyncSetUp
    asyncTearDown = support.ExecutionServiceTest.asyncTearDown
    submit = support.ExecutionServiceTest.submit
    event = support.ExecutionServiceTest.event

    async def active(self):
        row, _ = await self.submit()
        await self.client.starting.wait()
        await self.event(row["id"], "run.started", run={"id": "turn"})
        self.client.release.set()
        return row["id"]

    async def test_dedup_and_observed_consumption(self):
        ident = await self.active()
        args = {"id": ident, "message_id": str(uuid.uuid4()), "text": "same input"}
        first = await self.service.request(ExecutionRequest("supplement", args))
        self.assertEqual(first["state"], "accepted")
        await self.service.request(ExecutionRequest("supplement", args))
        self.assertEqual(sum(name == "run.steer" for name, _ in self.client.calls), 1)
        with self.assertRaisesRegex(ValueError, "different"):
            await self.service.request(ExecutionRequest("supplement", {**args, "text": "changed"}))
        session = self.service.records.get(ident)["data"]["session_id"]
        self.service.admission.inputs_consumed(session, "wrong", "model-request", [args["message_id"]])
        self.assertEqual(self.service.records.messages.get(args["message_id"])["state"], "accepted")
        self.service.admission.inputs_consumed(session, "turn", "model-request", [args["message_id"]])
        receipt = await self.service.request(
            ExecutionRequest(
                "message_get",
                {
                    "id": ident,
                    "message_id": args["message_id"],
                },
            )
        )
        self.assertEqual(receipt["state"], "consumed")
        self.assertEqual(receipt["model_request_id"], "model-request")
        self.assertIsNotNone(receipt["consumed_at"])
        await self.event(ident, "run.completed", run={"id": "turn", "status": "completed"})
        self.assertEqual(
            (await self.service.request(ExecutionRequest("supplement", args)))["state"], "consumed"
        )

    async def test_lost_send_response_and_restart_do_not_replay(self):
        ident = await self.active()
        original = self.client.request

        async def lost(command, args, timeout=30):
            result = await original(command, args, timeout)
            if command == "run.steer":
                raise ConnectionError("lost acknowledgement")
            return result

        self.client.request = lost
        args = {"id": ident, "message_id": str(uuid.uuid4()), "text": "once"}
        self.assertEqual(
            (await self.service.request(ExecutionRequest("supplement", args)))["state"], "unknown"
        )
        self.service.records.recover()
        self.assertEqual(
            (await self.service.request(ExecutionRequest("supplement", args)))["state"], "unknown"
        )
        self.assertEqual(sum(name == "run.steer" for name, _ in self.client.calls), 1)

    async def test_finished_input_not_consumed_and_cross_run_rejected(self):
        ident = await self.active()
        await self.event(ident, "run.completed", run={"id": "turn", "status": "completed"})
        args = {"id": ident, "message_id": str(uuid.uuid4()), "text": "too late"}
        self.assertEqual(
            (await self.service.request(ExecutionRequest("supplement", args)))["state"], "not_consumed"
        )
        self.service.records.create("other", "operator", "other", {"model": "test", "effort": "low"})
        with self.assertRaisesRegex(ValueError, "different execution"):
            await self.service.request(ExecutionRequest("supplement", {**args, "id": "other"}))
        with self.assertRaisesRegex(ValueError, "Unknown execution message"):
            await self.service.request(
                ExecutionRequest(
                    "message_get",
                    {
                        "id": "other",
                        "message_id": args["message_id"],
                    },
                )
            )
        self.assertFalse(any(name == "run.steer" for name, _ in self.client.calls))

    async def test_unobserved_queued_message_becomes_unknown_on_terminal(self):
        ident = await self.active()
        args = {"id": ident, "message_id": str(uuid.uuid4()), "text": "may reach next request"}
        await self.service.request(ExecutionRequest("supplement", args))
        await self.event(ident, "run.completed", run={"id": "turn", "status": "completed"})
        self.assertEqual(self.service.records.messages.get(args["message_id"])["state"], "unknown")

    async def test_answer_acknowledgement_cannot_clear_a_new_question(self):
        ident = await self.active()
        session = self.service.records.get(ident)["data"]["session_id"]

        def question(number):
            return {
                "id": number,
                "kind": "input.requested",
                "data": {
                    "session_id": session,
                    "run_id": "turn",
                    "tool_call_id": "tool-" + str(number),
                    "questions": [{"id": "q", "question": "Which?"}],
                },
            }

        await self.service.event(question(10))

        async def respond(number, payload):
            await self.service.event(question(11))

        self.client.respond = respond
        await self.service.request(
            ExecutionRequest(
                "answer",
                {
                    "id": ident,
                    "message_id": str(uuid.uuid4()),
                    "question_id": 10,
                    "text": "first answer",
                },
            )
        )
        row = self.service.records.get(ident)
        self.assertEqual((row["state"], row["data"]["question_id"]), ("waiting_user", 11))
        self.assertEqual(self.service.questions[ident]["id"], 11)

    async def test_input_bound_and_missing_identity(self):
        ident = await self.active()
        with self.assertRaisesRegex(ValueError, "message_id"):
            await self.service.request(ExecutionRequest("supplement", {"id": ident, "text": "no id"}))
        for index in range(64):
            await self.service.request(
                ExecutionRequest(
                    "supplement",
                    {
                        "id": ident,
                        "message_id": str(uuid.uuid4()),
                        "text": str(index),
                    },
                )
            )
        with self.assertRaisesRegex(RuntimeError, "queue is full"):
            await self.service.request(
                ExecutionRequest(
                    "supplement",
                    {
                        "id": ident,
                        "message_id": str(uuid.uuid4()),
                        "text": "overflow",
                    },
                )
            )


class EventWindowTest(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.records = RunRecords(self.db)
        self.records.create("a", "owner", "a", {})
        self.records.create("b", "owner", "b", {})

    def test_interleaving_does_not_create_false_gap_and_trim_is_explicit(self):
        with self.db:
            for n in range(520):
                self.records.event("b", "progress", {"n": n})
                if n < 2:
                    self.records.event("a", "progress", {"n": n})
        a, b = self.records.events("a"), self.records.events("b")
        self.assertFalse(a["gap"])
        self.assertTrue(a["history_complete"])
        self.assertTrue(b["gap"])
        self.assertGreater(b["deleted_through"], 0)
        self.assertEqual(len(b["items"]), 100)
        self.assertTrue(b["has_more"])
        self.assertFalse(b["history_complete"])
        cursor = b["cursor"]
        while b["has_more"]:
            b = self.records.events("b", cursor)
            self.assertFalse(b["gap"])
            cursor = b["cursor"]
        empty = self.records.events("b", cursor)
        self.assertEqual(empty["items"], [])
        self.assertEqual(empty["execution_state"], "queued")
        self.records.update("b", "completed")
        tail = self.records.events("b", cursor)
        self.assertEqual(tail["execution_state"], "completed")

    def test_migration_does_not_invent_complete_history(self):
        with self.db:
            self.db.execute("DROP TABLE agent_run_event_windows")
        migrated = RunRecords(self.db)
        self.assertFalse(migrated.events("a")["history_origin_known"])
        self.assertFalse(migrated.events("a")["history_complete"])
        migrated.create("c", "owner", "c", {})
        self.assertTrue(migrated.events("c")["history_complete"])
        self.assertFalse(RunRecords(self.db).events("a")["history_origin_known"])

    def test_oversize_preserves_association_and_future_cursor_is_rejected(self):
        with self.db:
            self.records.event("a", "operation.output", {"operation_id": "operation", "text": "x" * 20000})
        data = self.records.events("a")["items"][-1]["data"]
        self.assertTrue(data["truncated"])
        self.assertEqual(data["operation_id"], "operation")
        self.assertGreater(data["original_bytes"], 16384)
        with self.assertRaises(ValueError):
            self.records.events("a", 99999)


class PiInputEvidenceTest(unittest.TestCase):
    def test_only_matching_turn_role_body_and_tool_call_is_consumed(self):
        found = []
        worker = SimpleNamespace(
            meta={}, active_turn="turn", compacting=False, execution_session_id="session"
        )
        client = SimpleNamespace(
            _worker=lambda _: worker,
            _save_meta=lambda _: None,
            admission=SimpleNamespace(inputs_consumed=lambda *args: found.append(args)),
        )
        track(client, "session", "turn", "message", "toolResult", "answer", "call")
        for role, call in [("user", "call"), ("toolResult", "other")]:
            consumed(
                client,
                worker,
                {"messages": [{"role": role, "toolCallId": call, "content": "answer"}]},
                "model",
            )
        self.assertEqual(found, [])
        consumed(
            client,
            worker,
            {
                "messages": [
                    {
                        "role": "toolResult",
                        "toolCallId": "call",
                        "content": [{"type": "text", "text": "answer"}],
                    },
                ]
            },
            "model",
        )
        self.assertEqual(found, [("session", "turn", "model", ["message"])])
        self.assertEqual(worker.meta["public_inputs"], {})


class OperationEvidenceTest(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.records = RunRecords(self.db)
        self.records.create("run", "owner", "run", {"model": "test", "effort": "auto"})
        self.ops = self.records.operations
        self.addCleanup(self.db.close)

    def test_unknown_keeps_late_evidence_and_never_unlocks_legacy_writes(self):
        model = self.ops.begin("run", "model", "usage-ticket", turn_id="turn")
        tool = self.ops.begin("run", "tool", "native-tool", model_request_id="usage-ticket", tool="write")
        self.assertEqual(self.ops.begin("run", "tool", "native-tool"), tool)
        self.assertEqual(
            self.db.execute("SELECT parent_id FROM execution_operations WHERE id=?", (tool,)).fetchone()[0],
            model,
        )
        self.records.update("run", "unknown")
        self.ops.settle("run", "model", "usage-ticket", "completed")
        self.assertEqual(
            self.db.execute("SELECT state FROM execution_operations WHERE id=?", (model,)).fetchone()[0],
            "unknown",
        )
        self.assertEqual(self.records.get("run")["state"], "unknown")
        result = self.ops.reconcile("run")
        self.assertFalse(result["safe_to_retry"])
        self.assertEqual(result["local_writes"], "unfenced")
        history = self.ops.history("run")
        self.assertTrue(any(item.get("late") for item in history["items"]))
        self.assertTrue(all(item["host_start_id"] for item in history["items"]))
        with self.assertRaisesRegex(RuntimeError, "no longer"):
            self.ops.begin("run", "model", "new-ticket")

    def test_recovery_is_idempotent_and_observations_are_paginated(self):
        self.records.update("run", "running")
        self.ops.begin("run", "tool", "one", tool="external")
        self.records.recover()
        restored = RunRecords(self.db)
        restored.recover()
        page = restored.operations.history("run", limit=1)
        self.assertTrue(page["has_more"])
        rest = restored.operations.history("run", cursor=page["cursor"])
        self.assertEqual(len(rest["items"]), 1)
        self.assertEqual(rest["items"][0]["state"], "unknown")
        old_boot = rest["items"][0]["host_start_id"]
        restored.operations.reconcile("run")
        last = restored.operations.history("run")["items"][-1]
        self.assertNotEqual(old_boot, last["host_start_id"])
