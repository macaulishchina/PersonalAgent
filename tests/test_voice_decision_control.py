"""Versioned voice confirmation, isolated targets and no uncertain action replay."""

import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from assistant_runtime.interaction.voice_control import VoiceControl
from assistant_runtime.kernel.contracts import VoiceDecisionRequest, TaskRequest
from assistant_runtime.plugins.codex.backend import CodexBackend


class VoiceControlTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.host = SimpleNamespace(
            context=SimpleNamespace(spawn=lambda work, name: asyncio.create_task(work)),
            active=True,
            quiet_active=lambda: False,
            voice=SimpleNamespace(epoch=2),
            input_count=3,
            conversation=SimpleNamespace(
                voice_revision=4, feedback=SimpleNamespace(get=lambda _: {"source": "voice", "epoch": 2})
            ),
            backend=SimpleNamespace(
                active_turn="turn",
                import_voice_messages=AsyncMock(),
                control_checkpoint=AsyncMock(return_value=True),
                cancel=AsyncMock(),
            ),
            runtime=SimpleNamespace(invoke=AsyncMock(return_value={"active_runs": [{"id": "run"}]})),
        )
        self.owner = VoiceControl(self.host)
        self.result = {"status": "ok", "decision_id": "a" * 32, "choice": "correct"}

    async def offer(self):
        await self.owner.offer(self.result, "不是第一季，是第二季", 2, 3, "turn", 4)

    async def confirm(self, **changes):
        return await self.owner.request(
            VoiceDecisionRequest("confirm", {"decision_id": "a" * 32, "accept": True, **changes})
        )

    async def test_suggestion_does_not_stop_or_redispatch_until_model_confirmation(self):
        await self.offer()
        self.host.backend.cancel.assert_not_awaited()
        self.assertFalse(
            any(isinstance(c.args[0], TaskRequest) for c in self.host.runtime.invoke.await_args_list)
        )
        result = await self.confirm()
        self.assertEqual(result["state"], "applied")
        self.host.backend.control_checkpoint.assert_awaited_once_with("turn")
        self.assertFalse((await self.confirm())["replayed"])
        self.host.backend.control_checkpoint.assert_awaited_once()

    async def test_new_input_during_foreground_checkpoint_rejects_confirmation(self):
        async def checkpoint(*args, **kwargs):
            self.host.input_count += 1
            return True

        self.host.backend.control_checkpoint.side_effect = checkpoint
        await self.offer()
        self.assertEqual((await self.confirm())["state"], "expired")

    async def test_confirmation_waits_for_successful_context_delivery(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def inject(messages):
            entered.set()
            await release.wait()

        self.host.backend.import_voice_messages.side_effect = inject
        delivering = asyncio.create_task(self.offer())
        try:
            await asyncio.wait_for(entered.wait(), 1)
            self.assertEqual((await self.confirm())["state"], "delivering")
            self.host.backend.control_checkpoint.assert_not_awaited()
            self.host.input_count += 1
        finally:
            release.set()
            await delivering
        self.assertEqual((await self.confirm())["state"], "unavailable")
        self.host.backend.control_checkpoint.assert_not_awaited()

    async def test_failed_suggestion_delivery_is_optional_and_cannot_be_confirmed(self):
        self.host.backend.import_voice_messages.side_effect = TimeoutError()
        await self.offer()
        self.assertEqual((await self.confirm())["state"], "unavailable")
        self.host.backend.control_checkpoint.assert_not_awaited()

    async def test_active_web_turn_does_not_target_previous_voice_task(self):
        await self.owner.bind("voice-turn", "task")
        self.host.conversation.feedback.get = lambda _: {"source": "text", "epoch": 2}
        self.host.backend.active_turn = "web-turn"
        self.assertIsNone(self.owner.capture_turn())

    async def test_takeover_during_pause_prevents_old_correction_from_resuming(self):
        await self.owner.bind("turn", "task")
        pausing, release = asyncio.Event(), asyncio.Event()
        actions = []

        async def invoke(request, **kwargs):
            if isinstance(request, TaskRequest):
                actions.append(request.action)
                if request.action == "get":
                    return {"active_runs": [{"id": "run", "state": "running"}]}
                if request.action == "pause_run":
                    pausing.set()
                    await release.wait()
                    return {"state": "paused"}
                if request.action == "cancel_run":
                    return {"state": "canceled"}
                self.fail("Old correction must not resume after takeover")
            return {}

        self.host.runtime.invoke.side_effect = invoke
        await self.offer()
        confirming = asyncio.create_task(self.confirm())
        try:
            await asyncio.wait_for(pausing.wait(), 1)
            self.assertTrue(await self.owner.stop("turn"))
        finally:
            release.set()
        self.assertEqual((await confirming)["state"], "expired")
        self.assertNotIn("continue", actions)

    async def test_stale_input_and_background_caller_cannot_confirm(self):
        await self.offer()
        with self.assertRaises(ValueError):
            await self.owner.request(VoiceDecisionRequest("get", caller_run="background"))
        self.host.input_count += 1
        self.assertEqual((await self.confirm())["state"], "unavailable")
        self.host.backend.control_checkpoint.assert_not_awaited()

    async def test_correction_pauses_only_bound_run_then_continues_without_archiving_plan(self):
        await self.owner.bind("turn", "task")
        calls = []

        async def invoke(request, **kwargs):
            if isinstance(request, TaskRequest):
                calls.append(request)
                if request.action == "get":
                    return {"active_runs": [{"id": "run", "state": "running"}]}
                if request.action == "pause_run":
                    return {"id": "run", "state": "paused"}
                return {"id": "run", "state": "queued"}
            return {}

        self.host.runtime.invoke.side_effect = invoke
        await self.offer()
        self.assertEqual((await self.confirm())["state"], "applied")
        self.assertEqual([r.action for r in calls], ["get", "pause_run", "continue"])
        self.assertEqual(calls[-1].arguments["run_id"], "run")
        self.assertEqual(calls[-1].arguments["text"], "不是第一季，是第二季")
        await self.confirm()
        self.assertEqual(len(calls), 3)

    async def test_unknown_stop_never_starts_replacement_or_retries(self):
        await self.owner.bind("turn", "task")
        actions = []

        async def invoke(request, **kwargs):
            if isinstance(request, TaskRequest):
                actions.append(request.action)
                if request.action == "get":
                    return {"active_runs": [{"id": "run", "state": "running"}]}
                raise TimeoutError()
            return {}

        self.host.runtime.invoke.side_effect = invoke
        await self.offer()
        self.assertEqual((await self.confirm())["state"], "unknown")
        self.assertEqual((await self.confirm())["state"], "unknown")
        self.assertEqual(actions, ["get", "pause_run"])

    async def test_new_input_during_stop_prevents_continuation(self):
        await self.owner.bind("turn", "task")
        actions = []

        async def invoke(request, **kwargs):
            if isinstance(request, TaskRequest):
                actions.append(request.action)
                if request.action == "get":
                    return {"active_runs": [{"id": "run", "state": "running"}]}
                self.host.input_count += 1
                return {"state": "paused"}
            return {}

        self.host.runtime.invoke.side_effect = invoke
        await self.offer()
        self.assertEqual((await self.confirm())["state"], "expired")
        self.assertEqual(actions, ["get", "pause_run"])

    async def test_multiple_associated_tasks_require_clarification(self):
        await self.owner.bind("turn", "task-one")
        await self.owner.bind("turn", "task-two")
        self.host.runtime.invoke.reset_mock()
        await self.offer()
        self.assertEqual((await self.confirm())["state"], "clarification")
        self.assertFalse(
            any(isinstance(c.args[0], TaskRequest) for c in self.host.runtime.invoke.await_args_list)
        )

    async def test_supplement_uses_bound_run_without_interrupt(self):
        await self.owner.bind("turn", "task")
        self.result["choice"] = "supplement"
        actions = []

        async def invoke(request, **kwargs):
            if isinstance(request, TaskRequest):
                actions.append(request.action)
                return {"active_runs": [{"id": "run", "state": "waiting_user"}]}
            return {}

        self.host.runtime.invoke.side_effect = invoke
        await self.offer()
        self.assertEqual((await self.confirm())["state"], "applied")
        self.assertEqual(actions, ["get", "supplement"])

    async def test_original_run_cannot_be_replaced_by_next_scheduled_run(self):
        await self.owner.bind("turn", "task")
        self.host.runtime.invoke.return_value = {"active_runs": [{"id": "later-run"}]}
        await self.offer()
        self.assertEqual((await self.confirm())["state"], "clarification")
        self.assertFalse(await self.owner.stop("turn"))
        actions = [
            c.args[0].action
            for c in self.host.runtime.invoke.await_args_list
            if isinstance(c.args[0], TaskRequest)
        ]
        self.assertEqual(actions, ["get", "get", "get"])

    async def test_foreground_stop_requires_readback_not_interrupt_ack(self):
        self.host.backend.control_checkpoint.return_value = False
        self.assertFalse(await self.owner.stop("turn"))
        self.host.backend.cancel.assert_awaited_once_with(expected_turn="turn")
        self.host.backend.active_turn = "other-turn"
        self.assertFalse(await self.owner.stop("turn"))
        self.host.backend.cancel.assert_awaited_once()

    async def test_cancelled_confirmation_is_not_replayed(self):
        self.host.backend.control_checkpoint.side_effect = asyncio.CancelledError()
        await self.offer()
        with self.assertRaises(asyncio.CancelledError):
            await self.confirm()
        self.assertFalse((await self.confirm())["replayed"])
        self.host.backend.control_checkpoint.assert_awaited_once()


class CheckpointTest(unittest.IsolatedAsyncioTestCase):
    async def test_active_or_unknown_side_effects_prevent_checkpoint(self):
        backend = CodexBackend.__new__(CodexBackend)
        backend.thread_id, backend.active_turn = "thread", "turn"
        record = {"id": "turn", "status": "inProgress", "items": []}

        async def rpc(method, params):
            return {"thread": {"turns": [record]}} if method == "thread/read" else {"data": []}

        backend.client = SimpleNamespace(request=AsyncMock(side_effect=rpc))
        self.assertTrue(await backend.control_checkpoint("turn"))
        record["items"] = [{"type": "mcpToolCall", "status": "inProgress", "server": "external"}]
        self.assertFalse(await backend.control_checkpoint("turn"))
        record["items"][0]["status"] = "completed"
        self.assertFalse(await backend.control_checkpoint("turn", stopped=True))
        record["status"] = "interrupted"
        self.assertTrue(await backend.control_checkpoint("turn", stopped=True))
