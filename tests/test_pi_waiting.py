"""Cancellation of real Pi clarification waits; synthetic local model only."""

import asyncio
from pathlib import Path
import tempfile
import unittest
import uuid

from assistant_runtime.api import ConversationRequest
from assistant_runtime.api.contracts import TaskRequest
from assistant_runtime.app.config import Config
from assistant_runtime.app.host import AssistantService
from assistant_runtime.core.pi.rpc import RpcError
from pi_gateway import sse_text_reply, sse_tool_call
import test_pi_skeleton as pi_fixtures


class WaitingInputTest(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = pi_fixtures.PiSkeletonTest.asyncSetUp
    asyncTearDown = pi_fixtures.PiSkeletonTest.asyncTearDown
    session = pi_fixtures.PiSkeletonTest.session
    completed = pi_fixtures.PiSkeletonTest.completed

    async def waiting(self):
        self.gateway.script_fn = lambda step, body: sse_tool_call(
            "ask", "request_user_input", {"questions": [{"id": "q", "question": "Confirm material?"}]}
        )
        session = await self.session()
        run = await self.client.request(
            "run.start", {"session_id": session, "input": [{"type": "text", "text": "Ask then wait"}]}
        )
        async with asyncio.timeout(15):
            while True:
                event = await self.client.events.get()
                if event["kind"] == "input.requested":
                    return session, run["run"]["id"], event

    async def test_cancel_waiting_input_is_confirmed_and_does_not_replay_after_restart(self):
        session, turn, question = await self.waiting()
        args = {"session_id": session, "run_id": turn}
        await self.client.request("run.cancel", args)
        self.assertEqual((await self.completed())["status"], "interrupted")
        self.assertNotIn(question["id"], self.client.ui_requests)
        with self.assertRaisesRegex(RpcError, "no longer active"):
            await self.client.respond(question["id"], {"answers": {}})
        self.assertIsNone(self.client.workers[session].active_turn)
        self.assertFalse(self.client.workers[session].meta.get("pending_operations"))
        await self.client.request("run.cancel", args)
        self.assertEqual(len(self.gateway.requests), 1)
        await self.client.close()
        await self.client.start()
        saved = await self.client.request("session.read", {"session_id": session, "include_runs": True})
        self.assertEqual(saved["session"]["runs"][-1]["status"], "interrupted")
        self.assertEqual(len(self.gateway.requests), 1)

    async def test_cancel_and_answer_race_cannot_leave_a_waiting_tool(self):
        session, turn, question = await self.waiting()
        self.gateway.script_fn = lambda step, body: sse_text_reply("final", "ANSWERED")
        results = await asyncio.gather(
            self.client.request("run.cancel", {"session_id": session, "run_id": turn}),
            self.client.respond(question["id"], {"answers": {"q": {"answers": ["confirmed"]}}}),
            return_exceptions=True,
        )
        self.assertFalse(isinstance(results[0], BaseException), results)
        self.assertEqual((await self.completed())["status"], "interrupted")
        self.assertNotIn(question["id"], self.client.ui_requests)
        self.assertLessEqual(len(self.gateway.requests), 2)


class TaskWaitingTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from pi_gateway import FakeResponsesGateway

        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / ".env").write_text("TEST_KEY=synthetic-key\n")
        self.gateway = FakeResponsesGateway(
            lambda step, body: sse_tool_call(
                f"ask-{step}",
                "request_user_input",
                {"questions": [{"id": "q", "question": "Confirm material?"}]},
            )
        )
        self.config = Config(
            root=self.root,
            workspace=str(self.root),
            plugins=[
                "ai_usage",
                "models",
                "workfiles",
                "pi",
                "execution",
                "conversation",
                "notifications",
                "tasks",
            ],
            main_model="model-a",
            main_reasoning_effort="auto",
            model_api=pi_fixtures.configuration(self.gateway.base_url),
            execution={"concurrency": 4},
            tasks={"model_policy": {"review_mode": "off"}},
        )
        await self.start()

    async def start(self):
        self.host = AssistantService(self.config)
        self.running = asyncio.create_task(self.host.run())
        await asyncio.wait_for(self.host.runtime.ready.wait(), 30)

    async def stop(self):
        self.host.stop_event.set()
        await asyncio.wait_for(self.running, 30)

    async def asyncTearDown(self):
        await self.stop()
        await self.gateway.aclose()
        self.temp.cleanup()

    async def call(self, action, **args):
        return await self.host.runtime.invoke(TaskRequest(action, args))

    async def wait(self, task, states):
        async with asyncio.timeout(20):
            while True:
                saved = await self.call("get", task_id=task["id"])
                if saved["runs"] and saved["runs"][0]["state"] in states:
                    return saved["runs"][0]
                await asyncio.sleep(0.02)

    async def create(self):
        task = await self.call(
            "create",
            request_id=str(uuid.uuid4()),
            spec={
                "title": "Synthetic waiting task",
                "prompt": "Ask for confirmation then wait",
                "execution": {"model": "model-a", "effort_mode": "model_default"},
                "tools": ["request_user_input"],
                "notify_completion": False,
                "notify_voice": False,
                "auto_archive": False,
            },
        )
        return task, await self.wait(task, {"waiting_user"})

    def assert_released(self, run):
        self.assertEqual(run["data"]["questions"], [])
        self.assertFalse(run["data"]["turn_active"])
        execution = run["data"]["execution_id"]
        service = self.host.backend.executions
        self.assertNotIn(execution, service.questions)
        self.assertNotIn(execution, service.admission.leases)
        self.assertNotIn(run["id"], self.host.backend.jobs.questions)
        receipt = service.records.get(execution)
        self.assertEqual(receipt["state"], "cancelled")
        self.assertEqual(receipt["data"]["questions"], [])
        self.assertIsNone(receipt["data"]["question_id"])

    async def test_public_cancel_is_targeted_repeated_and_durable(self):
        first, run = await self.create()
        second, other = await self.create()
        async with asyncio.timeout(5):
            ended = await self.call("cancel_run", run_id=run["id"])
        self.assertEqual(ended["state"], "canceled")
        self.assert_released(ended)
        self.assertEqual((await self.call("cancel_run", run_id=run["id"]))["state"], "canceled")
        self.assertEqual((await self.wait(second, {"waiting_user"}))["id"], other["id"])
        self.assertEqual(len(self.host.backend.client.ui_requests), 1)
        self.gateway.script_fn = lambda step, body: sse_text_reply(f"done-{step}", "CONFIRMED")
        chat = await self.host.runtime.invoke(ConversationRequest("create", {"title": "Foreground"}))
        request = str(uuid.uuid4())
        await self.host.runtime.invoke(
            ConversationRequest(
                "submit", {"session_id": chat["id"], "request_id": request, "text": "Continue"}
            )
        )
        async with asyncio.timeout(20):
            while True:
                receipt = await self.host.runtime.invoke(
                    ConversationRequest("receipt", {"session_id": chat["id"], "request_id": request})
                )
                if receipt["found"] and receipt["state"] == "completed":
                    break
                await asyncio.sleep(0.02)
        await self.call("supplement", run_id=other["id"], text="Confirmed")
        self.assertEqual((await self.wait(second, {"completed"}))["data"]["result"], "CONFIRMED")
        self.assertFalse(self.host.backend.client.ui_requests)
        self.assertEqual(len(self.gateway.requests), 4)
        await self.stop()
        await self.start()
        self.assertEqual((await self.wait(first, {"canceled"}))["state"], "canceled")
        self.assertEqual((await self.wait(second, {"completed"}))["state"], "completed")
        self.assertEqual(len(self.gateway.requests), 4)

    async def test_public_pause_waiting_releases_resources(self):
        _, run = await self.create()
        ended = await self.call("pause_run", run_id=run["id"])
        self.assertEqual(ended["state"], "paused")
        self.assert_released(ended)
        self.assertFalse(self.host.backend.client.ui_requests)
        self.assertEqual(len(self.gateway.requests), 1)

    async def test_public_answer_racing_cancel_has_one_terminal_outcome(self):
        task, run = await self.create()
        self.gateway.script_fn = lambda step, body: sse_text_reply(f"done-{step}", "CONFIRMED")
        await asyncio.gather(
            self.call("cancel_run", run_id=run["id"]),
            self.call("supplement", run_id=run["id"], text="Confirmed"),
            return_exceptions=True,
        )
        ended = await self.wait(task, {"canceled", "completed", "unknown"})
        self.assertIn(ended["state"], {"canceled", "completed"})
        self.assertFalse(ended["data"]["questions"])
        self.assertFalse(self.host.backend.client.ui_requests)
        self.assertNotIn(ended["data"]["execution_id"], self.host.backend.executions.admission.leases)
        self.assertLessEqual(len(self.gateway.requests), 2)
