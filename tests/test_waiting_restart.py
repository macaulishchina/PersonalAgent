"""Public waiting-task recovery with real pinned Pi and private synthetic models."""

import asyncio
import json
from dataclasses import asdict
from pathlib import Path
import sys
import unittest
import uuid
from unittest.mock import patch

from assistant_runtime.api import ConversationRequest
from assistant_runtime.api.execution import Command
from assistant_runtime.api.models import CapabilityRequestError
from assistant_runtime.api.system import system
from pi_gateway import sse_text_reply, sse_tool_call, sse_tool_batch
import test_pi_waiting as fixtures


class WaitingRestartTest(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.TaskWaitingTest.asyncSetUp
    asyncTearDown = fixtures.TaskWaitingTest.asyncTearDown
    start = fixtures.TaskWaitingTest.start
    stop = fixtures.TaskWaitingTest.stop
    call = fixtures.TaskWaitingTest.call
    wait = fixtures.TaskWaitingTest.wait

    async def create(self, model="model-a", **spec):
        task = await self.call(
            "create",
            request_id=str(uuid.uuid4()),
            spec={
                "title": "Synthetic waiting recovery",
                "prompt": "Ask for confirmation then wait",
                "execution": {"model": model, "effort_mode": "model_default"},
                "tools": ["request_user_input"],
                "notify_completion": False,
                "notify_voice": False,
                "auto_archive": False,
                **spec,
            },
        )
        return task, await self.wait(task, {"waiting_user"})

    async def test_clean_restart_restores_identity_without_inference_then_answers_once(self):
        first, before = await self.create()
        second, canceled = await self.create()
        self.assertEqual(before["data"]["recovery"]["state"], "waiting_user")
        question = before["data"]["question_id"]
        count = len(self.gateway.requests)
        await self.stop()
        await self.start()
        restored = await self.wait(first, {"waiting_user", "unknown"})
        self.assertEqual(restored["state"], "waiting_user", restored["data"])
        self.assertEqual(restored["data"]["question_id"], question)
        self.assertTrue(restored["data"]["recovery"]["restored"])
        self.assertEqual(restored["data"]["turn_id"], before["data"]["turn_id"])
        self.assertEqual(restored["data"]["execution_id"], before["data"]["execution_id"])
        self.assertEqual(len(self.gateway.requests), count)
        with self.assertRaisesRegex(RuntimeError, "another run"):
            await self.host.backend.client._request(
                Command.CANCEL,
                {
                    "session_id": restored["data"]["execution_session_id"],
                    "run_id": "stale-turn",
                },
            )
        await self.call("cancel_run", run_id=canceled["id"])
        self.assertEqual((await self.wait(second, {"canceled"}))["state"], "canceled")
        self.gateway.script_fn = lambda step, body: sse_text_reply(f"done-{step}", "CONFIRMED")
        message = str(uuid.uuid4())
        args = dict(
            run_id=restored["id"], text="Confirmed unique answer", question_id=question, message_id=message
        )
        response = await self.call("supplement", **args)
        self.assertIn(response["input_receipt"]["state"], {"accepted", "consumed"})
        ended = await self.wait(first, {"completed", "unknown", "failed"})
        self.assertEqual(ended["state"], "completed", ended["data"])
        self.assertEqual(ended["data"]["turn_id"], before["data"]["turn_id"])
        replay = await self.call("supplement", **args)
        self.assertEqual(replay["input_receipt"]["state"], "consumed")
        self.assertEqual(len(self.gateway.requests), count + 1)
        self.assertIn("Confirmed unique answer", json.dumps(self.gateway.requests[-1]))
        with self.assertRaises(CapabilityRequestError) as expired:
            await self.call("supplement", **(args | {"message_id": str(uuid.uuid4())}))
        self.assertEqual(expired.exception.code, "question_expired")
        with self.assertRaisesRegex(ValueError, "different"):
            await self.call("supplement", **(args | {"text": "Changed answer"}))
        count = len(self.gateway.requests)
        # Later task rounds may point at a different canonical execution. Lookup
        # follows the durable answer identity, while retaining task authorization.
        service = self.host.backend.executions
        old_receipt = await service.answer_task(
            str(uuid.uuid4()),
            ended["id"],
            message_id=message,
            question_id=question,
            text=args["text"],
        )
        self.assertEqual(old_receipt["state"], "consumed")
        with self.assertRaises(PermissionError):
            await service.answer_task(
                str(uuid.uuid4()), "another-task", message_id=message, question_id=question, text=args["text"]
            )
        self.assertEqual(len(self.gateway.requests), count)

    async def answer(self, run, **changes):
        return await self.call(
            "supplement",
            run_id=run["id"],
            text="Explicit answer",
            message_id=changes.pop("message_id", str(uuid.uuid4())),
            question_id=changes.pop("question_id", run["data"]["question_id"]),
            **changes,
        )

    async def test_abrupt_host_exit_restores_two_models_without_dispatch(self):
        await self.stop()
        self.config.model_api["entries"]["model-b"]["default_reasoning_effort"] = "high"
        config = self.root / "crash-config.json"
        config.write_text(json.dumps(asdict(self.config), default=str), encoding="utf-8")
        child = await system.spawn(
            sys.executable,
            "-B",
            str(Path(__file__).with_name("waiting_host.py")),
            str(config),
            stdout=-1,
            stderr=-1,
        )
        try:
            async with asyncio.timeout(40):
                while True:
                    line = await child.stdout.readline()
                    if not line:
                        self.fail((await child.stderr.read()).decode(errors="replace"))
                    if line.startswith(b"WAITING_TEST:"):
                        tasks = json.loads(line[len(b"WAITING_TEST:") :])
                        break
        finally:
            await system.stop_process(child)
        self.assertEqual(len(self.gateway.requests), 2)
        await self.start()
        for item in tasks:
            restored = await self.wait(item["task"], {"waiting_user", "unknown"})
            self.assertEqual(restored["state"], "waiting_user", restored["data"])
            for key in ("turn_id", "execution_id", "question_id"):
                self.assertEqual(restored["data"][key], item["run"]["data"][key])
        self.assertEqual(len(self.gateway.requests), 2)
        self.gateway.script_fn = lambda step, body: sse_text_reply(f"done-{step}", "AFTER_CRASH")
        await self.answer(tasks[1]["run"])
        self.assertEqual((await self.wait(tasks[1]["task"], {"completed", "failed"}))["state"], "completed")
        self.assertEqual(self.gateway.requests[-1]["model"], "model-b")
        await self.call("cancel_run", run_id=tasks[0]["run"]["id"])
        self.assertEqual((await self.wait(tasks[0]["task"], {"canceled"}))["state"], "canceled")
        self.assertEqual(len(self.gateway.requests), 3)

    async def unavailable_checkpoint(self, damage, code):
        task, run = await self.create()
        meta_path = self.host.backend.client._meta_path(run["data"]["execution_session_id"])
        await self.stop()
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        damage(meta, meta_path)
        await self.start()
        restored = await self.wait(task, {"unknown", "waiting_user"})
        self.assertEqual(restored["state"], "unknown", restored["data"])
        self.assertEqual(restored["data"]["recovery"]["code"], code)
        self.assertEqual(len(self.gateway.requests), 1)
        with self.assertRaises(CapabilityRequestError) as rejected:
            await self.answer(run)
        self.assertEqual(rejected.exception.code, "question_expired")

    async def test_changed_history_stays_unknown(self):
        def damage(meta, _):
            with Path(meta["path"]).open("ab") as stream:
                stream.write(b"{}\n")

        await self.unavailable_checkpoint(damage, "waiting_checkpoint_corrupt")

    async def test_missing_history_stays_unknown(self):
        def damage(meta, _):
            Path(meta["path"]).rename(Path(meta["path"]).with_suffix(".unavailable"))

        await self.unavailable_checkpoint(damage, "waiting_checkpoint_unavailable")

    async def test_legacy_wait_is_not_fabricated_into_checkpoint(self):
        def damage(meta, path):
            meta.pop("input_checkpoint")
            path.write_text(json.dumps(meta), encoding="utf-8")

        await self.unavailable_checkpoint(damage, "waiting_checkpoint_unavailable")

    async def test_model_change_rejects_answer_before_accepting_or_inference(self):
        task, run = await self.create(model="model-b")
        await self.stop()
        self.config.model_api["entries"]["model-b"]["model"] = "different-upstream"
        await self.start()
        restored = await self.wait(task, {"waiting_user", "unknown"})
        self.assertEqual(restored["state"], "waiting_user")
        with self.assertRaises(CapabilityRequestError) as rejected:
            await self.answer(restored)
        self.assertEqual(rejected.exception.code, "waiting_model_unavailable")
        self.assertEqual(len(self.gateway.requests), 1)
        await self.call("cancel_run", run_id=run["id"])
        self.assertEqual((await self.wait(task, {"canceled"}))["state"], "canceled")

    async def test_uncertain_answer_is_not_replayed_after_restart(self):
        task, run = await self.create()
        await self.stop()
        await self.start()
        restored = await self.wait(task, {"waiting_user"})
        message = str(uuid.uuid4())
        with patch(
            "assistant_runtime.core.pi.waiting.native_context", side_effect=RuntimeError("Lost dispatch")
        ):
            response = await self.answer(restored, message_id=message)
        self.assertEqual(response["input_receipt"]["state"], "unknown")
        await self.stop()
        await self.start()
        saved = await self.wait(task, {"unknown"})
        self.assertEqual(saved["data"]["recovery"]["code"], "waiting_answer_unconfirmed")
        replay = await self.answer(restored, message_id=message)
        self.assertEqual(replay["input_receipt"]["state"], "unknown")
        self.assertEqual(len(self.gateway.requests), 1)

    async def test_waiting_tasks_release_model_capacity_for_foreground(self):
        await self.stop()
        self.config.execution["concurrency"] = 1
        await self.start()
        first, before = await self.create()
        second, _ = await self.create()
        await self.stop()
        await self.start()
        self.gateway.script_fn = lambda step, body: sse_text_reply(f"chat-{step}", "FOREGROUND")
        chat = await self.host.runtime.invoke(ConversationRequest("create", {"title": "Foreground"}))
        request = str(uuid.uuid4())
        await self.host.runtime.invoke(
            ConversationRequest(
                "submit", {"session_id": chat["id"], "request_id": request, "text": "Independent foreground"}
            )
        )
        async with asyncio.timeout(20):
            while True:
                receipt = await self.host.runtime.invoke(
                    ConversationRequest("receipt", {"session_id": chat["id"], "request_id": request})
                )
                if receipt.get("state") == "completed":
                    break
                await asyncio.sleep(0.02)
        self.assertEqual(
            (await self.wait(first, {"waiting_user"}))["data"]["question_id"], before["data"]["question_id"]
        )
        self.assertEqual((await self.wait(second, {"waiting_user"}))["state"], "waiting_user")

    async def test_historical_write_is_not_repeated_by_answer_continuation(self):
        target = self.root / "write-once.txt"

        def script(step, body):
            if step == 1:
                return sse_tool_call("write-once", "write", {"path": str(target), "content": "ONCE"})
            if step == 2:
                return sse_tool_call(
                    "ask", "request_user_input", {"questions": [{"id": "q", "question": "Proceed?"}]}
                )
            return sse_text_reply("done", "HISTORY_PRESERVED")

        self.gateway.script_fn = script
        task, run = await self.create(tools=["write", "request_user_input"])
        modified = target.stat().st_mtime_ns
        await self.stop()
        await self.start()
        restored = await self.wait(task, {"waiting_user"})
        await self.answer(restored)
        self.assertEqual((await self.wait(task, {"completed", "failed"}))["state"], "completed")
        self.assertEqual(target.read_text(), "ONCE")
        self.assertEqual(target.stat().st_mtime_ns, modified)
        self.assertEqual(len(self.gateway.requests), 3)
        self.assertEqual(self.gateway.requests[-1]["reasoning"]["effort"], "low")
        self.assertEqual(
            (await self.call("get", task_id=task["id"]))["runs"][0]["data"]["ai_usage"]["requests"], 3
        )

    async def test_missing_work_area_rejects_restore_without_inference(self):
        def damage(meta, _):
            Path(meta["cwd"]).rename(Path(meta["cwd"]).with_name("expired-work-area"))

        await self.unavailable_checkpoint(damage, "waiting_resource_unavailable")

    async def test_multiple_unfinished_calls_do_not_form_a_resumable_wait(self):
        self.gateway.script_fn = lambda step, body: sse_tool_batch(
            [
                ("ask-first", "request_user_input", {"questions": [{"id": "q1", "question": "First?"}]}),
                ("ask-second", "request_user_input", {"questions": [{"id": "q2", "question": "Second?"}]}),
            ]
        )
        task, run = await self.create()
        self.assertEqual(run["data"]["recovery"]["code"], "waiting_boundary_incomplete")
        await self.stop()
        await self.start()
        saved = await self.wait(task, {"unknown", "waiting_user"})
        self.assertEqual(saved["state"], "unknown")
        self.assertEqual(saved["data"]["recovery"]["code"], "waiting_boundary_incomplete")
        self.assertEqual(len(self.gateway.requests), 1)

    async def test_other_unknown_operation_prevents_recovery(self):
        task, run = await self.create()
        execution = run["data"]["execution_id"]
        operations = self.host.backend.executions.records.operations
        operations.begin(execution, "tool", "uncertain-other-operation")
        operations.settle(execution, "tool", "uncertain-other-operation", "unknown")
        with self.assertRaises(CapabilityRequestError) as rejected:
            await self.answer(run)
        self.assertEqual(rejected.exception.code, "waiting_boundary_incomplete")
        await self.stop()
        await self.start()
        saved = await self.wait(task, {"unknown", "waiting_user"})
        self.assertEqual(saved["state"], "unknown")
        self.assertEqual(saved["data"]["recovery"]["code"], "waiting_boundary_incomplete")
        self.assertFalse(self.host.backend.client.ui_requests)
        self.assertEqual(len(self.gateway.requests), 1)

    async def test_restored_answer_cancel_race_has_one_terminal_event(self):
        task, run = await self.create()
        await self.stop()
        await self.start()
        saved = await self.wait(task, {"waiting_user"})
        self.gateway.script_fn = lambda step, body: sse_text_reply(f"done-{step}", "CONFIRMED")
        await asyncio.gather(
            self.answer(saved), self.call("cancel_run", run_id=run["id"]), return_exceptions=True
        )
        ended = await self.wait(task, {"completed", "canceled"})
        trace = await self.call("trace", run_id=run["id"])
        terminal = [
            e
            for e in trace["lifecycle"]["items"]
            if e["kind"] == "state" and e["data"].get("after") in {"completed", "canceled"}
        ]
        self.assertEqual(len(terminal), 1)
        self.assertIn(ended["state"], {"completed", "canceled"})
        self.assertLessEqual(len(self.gateway.requests), 2)
