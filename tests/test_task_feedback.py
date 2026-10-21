"""Explicit result ownership, lifecycle release and durable output (no timers/models)."""

import asyncio
import unittest
import uuid
from unittest.mock import AsyncMock, Mock, patch

import test_tasks as tasks
from assistant_runtime.conversation.service import Conversation
from assistant_runtime.kernel.contracts import ConversationFeedbackRequest, TaskRequest
from assistant_runtime.plugins.notifications.channels import ChatChannel
from assistant_runtime.plugins.tasks.feedback import TaskFeedback


class TaskFeedbackTest(unittest.IsolatedAsyncioTestCase):
    asyncTearDown = tasks.TasksTest.asyncTearDown

    async def asyncSetUp(self):
        await tasks.TasksTest.asyncSetUp(self)
        self.host.backend.session_id = "main"
        self.host.backend.active_turn = "foreground"
        self.host.voice.epoch = 1
        self.host.voice.finish_execution = Mock()
        self.c = self.host.conversation = Conversation(self.host)
        self.runtime.register("conversation", ConversationFeedbackRequest, self.c.task_replies.request)
        self.c.feedback.bind("foreground", "main", 1, 0, "web")

    async def new_run(self, owner="background", **spec):
        definition = await self.plugin.request(
            TaskRequest(
                "create",
                {
                    "request_id": str(uuid.uuid4()),
                    "feedback_owner": owner,
                    "spec": {"title": "test", "prompt": "authorized task", **spec},
                },
            )
        )
        if self.plugin.scheduler.get_job(definition["id"]):
            self.plugin.scheduler.remove_job(definition["id"])
        return self.plugin.repo.enqueue(definition, "feedback-test", "running")

    def complete(self, run):
        return self.plugin.repo.update_run(
            run["id"], "completed", progress="Verified task result", turn_active=False
        )

    def payload(self, run):
        return {
            "kind": "result",
            "event_key": f"task-result:{run['id']}:{run['data'].get('completion_round', 0)}",
            "title": "test · 本次执行结束",
            "body": "Verified task result",
            "voice": False,
        }

    async def transfer(self, run, owner, **overrides):
        info = self.plugin.feedback.describe(self.plugin.repo.run(run["id"]))
        return await self.plugin.request(
            TaskRequest(
                "transfer_feedback",
                {
                    "run_id": run["id"],
                    "round": info["round"],
                    "version": info["version"],
                    "owner": owner,
                    **overrides,
                },
            )
        )

    async def settled(self, run):
        for _ in range(40):
            current = self.plugin.repo.run(run["id"])
            if current["data"].get("completion_delivered"):
                return current
            await asyncio.sleep(0.1)
        self.fail(self.plugin.notification_error or "Completion feedback not settled")

    async def test_default_delegation_and_query_leave_background_responsible(self):
        run = await self.new_run()
        result = await self.plugin.request(TaskRequest("get", {"task_id": run["task_id"]}))
        self.assertEqual(result["runs"][0]["feedback"]["owner"], "background")
        self.complete(run)
        current = await self.settled(run)
        self.assertEqual(current["data"]["completion_feedback"]["route"], "notification")
        self.assertEqual(len(self.host.notifications.repo.list()["items"]), 1)

    async def test_explicit_conversation_result_is_immediate_without_model_reply(self):
        run = self.complete(await self.new_run("conversation"))
        current = await self.settled(run)
        receipt = current["data"]["completion_feedback"]
        self.assertEqual(receipt["route"], "conversation")
        message = self.c.store.get(receipt["message_id"])
        self.assertEqual(message["task_id"], run["task_id"])
        self.assertIn("Verified task result", message["text"])
        self.assertEqual(self.host.notifications.repo.unread_count(), 0)
        self.assertEqual(self.host.notifications.repo.list()["items"], [])

    async def test_take_back_then_hand_off_and_stale_transfer_conflict(self):
        run = await self.new_run()
        owned = await self.transfer(run, "conversation")
        self.assertTrue(owned["transferred"])
        with self.assertRaisesRegex(ValueError, "changed"):
            await self.transfer(run, "background", version=0)
        await self.plugin.request(TaskRequest("get", {"task_id": run["task_id"]}))
        self.assertEqual(self.plugin.feedback.describe(run)["owner"], "conversation")
        released = await self.transfer(run, "background")
        self.assertEqual(released["version"], 2)
        self.complete(run)
        current = await self.settled(run)
        self.assertEqual(current["data"]["completion_feedback"]["route"], "notification")

    async def test_finished_turn_releases_undelivered_result(self):
        run = await self.new_run("conversation")
        await self.c.task_done("foreground", "稍后告诉你结果", "completed")
        self.complete(run)
        current = await self.settled(run)
        self.assertEqual(current["data"]["completion_feedback"]["route"], "notification")

    async def test_new_input_and_restart_release_stale_conversation(self):
        for restart in (False, True):
            turn = "turn-" + str(restart)
            self.host.backend.active_turn = turn
            self.c.feedback.bind(turn, "main", 1, 0, "web")
            run = await self.new_run("conversation")
            if restart:
                self.c.feedback.recover("main")
            else:
                self.c.feedback.accept_input("new-input", turn, "main", 1, 1, "web")
            self.complete(run)
            current = await self.settled(run)
            self.assertEqual(current["data"]["completion_feedback"]["route"], "notification")

    async def test_delivered_result_cannot_be_recalled_or_reissued(self):
        run = self.complete(await self.new_run())
        await self.settled(run)
        transfer = await self.transfer(run, "conversation")
        self.assertFalse(transfer["transferred"])
        self.assertEqual(len(self.host.notifications.repo.list()["items"]), 1)

    async def test_lost_conversation_reply_retries_same_receipt_after_restart(self):
        run = self.complete(await self.new_run("conversation"))
        original = self.c.task_replies.request
        calls = 0

        async def lost(request):
            nonlocal calls
            result = await original(request)
            if request.action == "deliver":
                calls += 1
                if calls == 1:
                    raise TimeoutError("reply lost after persistence")
            return result

        self.runtime.handlers[ConversationFeedbackRequest] = ("conversation", lost)
        with self.assertRaises(TimeoutError):
            await self.plugin.feedback.deliver(run, self.payload(run))
        self.c.feedback.recover("main")
        self.plugin.feedback = TaskFeedback(self.plugin)
        receipt = await self.plugin.feedback.deliver(run, self.payload(run))
        self.assertEqual(receipt["route"], "conversation")
        self.assertEqual(len(self.c.store.history("main")), 1)
        self.assertEqual(self.host.notifications.repo.list()["items"], [])

    async def test_unavailable_capability_does_not_guess_delivery_or_change_owner(self):
        run = self.complete(await self.new_run("conversation"))
        self.runtime.handlers.pop(ConversationFeedbackRequest)
        with self.assertRaises(RuntimeError):
            await self.plugin.feedback.deliver(run, self.payload(run))
        self.assertEqual(self.plugin.feedback.describe(run)["state"], "committed")
        self.assertEqual(self.host.notifications.repo.list()["items"], [])
        self.runtime.handlers[ConversationFeedbackRequest] = ("conversation", self.c.task_replies.request)
        receipt = await self.plugin.feedback.deliver(run, self.payload(run))
        self.assertEqual(receipt["route"], "conversation")

    async def test_lost_notification_reply_does_not_duplicate_on_retry(self):
        run = self.complete(await self.new_run())
        publish = self.host.notifications.repo.publish
        calls = 0

        def lost(*args, **kwargs):
            nonlocal calls
            item = publish(*args, **kwargs)
            calls += 1
            if calls == 1:
                raise TimeoutError("lost receipt")
            return item

        with patch.object(self.host.notifications.repo, "publish", side_effect=lost):
            with self.assertRaises(TimeoutError):
                await self.plugin.feedback.deliver(run, self.payload(run))
            receipt = await self.plugin.feedback.deliver(run, self.payload(run))
        self.assertEqual(receipt["route"], "notification")
        self.assertEqual(len(self.host.notifications.repo.list()["items"]), 1)

    async def test_completion_commits_owner_before_late_takeover(self):
        run = self.complete(await self.new_run())
        await self.plugin.feedback.deliver(run, self.payload(run))
        result = await self.transfer(run, "conversation")
        self.assertFalse(result["transferred"])

    async def test_transfer_before_completion_routes_to_conversation(self):
        run = await self.new_run()
        await self.transfer(run, "conversation")
        self.complete(run)
        current = await self.settled(run)
        self.assertEqual(current["data"]["completion_feedback"]["route"], "conversation")

    async def test_new_round_and_new_occurrence_do_not_inherit_ownership(self):
        run = self.complete(await self.new_run("conversation"))
        await self.settled(run)
        newer = self.plugin.repo.update_run(
            run["id"], "running", completion_round=1, completion_delivered=False
        )
        self.assertEqual(self.plugin.feedback.describe(newer)["owner"], "background")
        with self.assertRaisesRegex(ValueError, "changed"):
            await self.transfer(newer, "conversation", round=0)
        definition = self.plugin.repo.definition(run["task_id"])
        second = self.plugin.repo.enqueue(definition, "second-occurrence", "running")
        self.assertEqual(self.plugin.feedback.describe(second)["owner"], "background")

    async def test_disabled_completion_and_background_cannot_take_foreground(self):
        run = await self.new_run(notify_completion=False)
        with self.assertRaisesRegex(ValueError, "disabled"):
            await self.transfer(run, "conversation")
        with self.assertRaisesRegex(ValueError, "Background"):
            await self.plugin.request(TaskRequest("transfer_feedback", {}, run["id"]))
        self.complete(run)
        current = await self.settled(run)
        self.assertEqual(current["data"]["completion_feedback"]["route"], "disabled")
        self.assertEqual(self.host.notifications.repo.list()["items"], [])

    async def test_invalid_claims_are_rejected_before_creating_task(self):
        for owner, spec in (
            ("fake", {}),
            ("conversation", {"notify_completion": False}),
            ("conversation", {"trigger": {"kind": "interval", "seconds": 600}}),
            ("conversation", {"long_term": True}),
        ):
            with self.assertRaises(ValueError):
                await self.new_run(owner, **spec)
        self.assertEqual(self.plugin.repo.definitions(), [])
        self.host.backend.active_turn = None
        with self.assertRaisesRegex(ValueError, "active"):
            await self.new_run("conversation")

    async def test_binding_captured_before_async_selection(self):
        validate = self.plugin.validate_spec

        async def switch(value, caller=None):
            spec = await validate(value, caller)
            self.c.feedback.bind("next", "main", 1, 1, "web")
            self.host.backend.active_turn = "next"
            return spec

        with patch.object(self.plugin, "validate_spec", side_effect=switch):
            run = await self.new_run("conversation")
        self.assertEqual(self.plugin.feedback.describe(run)["context"]["turn"], "foreground")
        self.complete(run)
        self.assertEqual((await self.settled(run))["data"]["completion_feedback"]["route"], "notification")

    async def test_voice_result_uses_existing_output_once(self):
        self.host.voice.opened = True
        self.host.backend.active_turn = "voice-turn"
        self.c.feedback.bind("voice-turn", "main", 1, 0, "voice")
        run = self.complete(await self.new_run("conversation"))
        current = await self.settled(run)
        self.assertEqual(current["data"]["completion_feedback"]["state"], "submitted")
        self.host.voice.say.assert_awaited_once()
        await self.plugin.feedback.deliver(self.plugin.repo.run(run["id"]), self.payload(run))
        self.host.voice.say.assert_awaited_once()

    async def test_voice_exit_releases_before_speech(self):
        self.host.voice.opened = True
        self.host.backend.active_turn = "voice-turn"
        self.c.feedback.bind("voice-turn", "main", 1, 0, "voice")
        run = await self.new_run("conversation")
        self.c.media_event({"kind": "closed"}, 1)
        self.complete(run)
        current = await self.settled(run)
        self.assertEqual(current["data"]["completion_feedback"]["route"], "notification")
        self.host.voice.say.assert_not_awaited()

    async def test_unknown_speech_creates_one_silent_notice_reusing_result_card(self):
        self.host.voice.opened = True
        self.host.backend.active_turn = "voice-turn"
        self.c.feedback.bind("voice-turn", "main", 1, 0, "voice")
        self.host.voice.say = AsyncMock(side_effect=OSError("output receipt lost"))
        run = self.complete(await self.new_run("conversation"))
        current = await self.settled(run)
        self.assertEqual(current["data"]["completion_feedback"]["route"], "notification")
        notices = self.host.notifications.repo.list()["items"]
        self.assertEqual(len(notices), 1)
        self.assertFalse(notices[0]["payload"]["voice"])
        await ChatChannel(self.host).deliver(notices[0])
        self.assertEqual(len(self.c.store.history("main")), 1)
        self.host.voice.say.assert_awaited_once()

    async def test_creation_retry_preserves_owner_and_rejects_changed_intent(self):
        args = {
            "request_id": str(uuid.uuid4()),
            "feedback_owner": "conversation",
            "spec": {"title": "retry", "prompt": "authorized"},
        }
        first = await self.plugin.request(TaskRequest("create", args))
        self.host.backend.active_turn = None
        second = await self.plugin.request(TaskRequest("create", args))
        self.assertEqual(first["id"], second["id"])
        with self.assertRaisesRegex(ValueError, "different"):
            await self.plugin.request(TaskRequest("create", {**args, "feedback_owner": "background"}))

    async def test_registered_images_remain_attached_to_conversation_result(self):
        run = await self.new_run("conversation")
        self.plugin.repo.update_run(run["id"], attachments=[{"id": "image"}])
        with patch.object(self.c.media, "resolve", return_value=[{"id": "image", "url": "/api/image"}]):
            self.complete(run)
            current = await self.settled(run)
        self.assertEqual(
            self.c.store.get(current["data"]["completion_feedback"]["message_id"])["attachments"][0]["id"],
            "image",
        )

    async def test_terminal_takeover_delivers_before_tool_returns(self):
        run = self.complete(await self.new_run())
        result = await self.transfer(run, "conversation")
        self.assertTrue(result["transferred"])
        self.assertEqual(result["state"], "delivered")
        self.assertIsNotNone(self.c.store.get(result["receipt"]["message_id"]))
        self.assertEqual(self.host.notifications.repo.list()["items"], [])

    async def test_independent_result_is_not_blocked_by_slow_speech(self):
        self.host.voice.opened = True
        self.host.backend.active_turn = "voice-turn"
        self.c.feedback.bind("voice-turn", "main", 1, 0, "voice")
        entered, release = asyncio.Event(), asyncio.Event()

        async def slow(*args, **kwargs):
            entered.set()
            await release.wait()

        self.host.voice.say = AsyncMock(side_effect=slow)
        first = self.complete(await self.new_run("conversation"))
        await asyncio.wait_for(entered.wait(), 3)
        second = self.complete(await self.new_run())
        try:
            current = await self.settled(second)
            self.assertEqual(current["data"]["completion_feedback"]["route"], "notification")
            self.assertFalse(self.plugin.repo.run(first["id"])["data"].get("completion_delivered", False))
        finally:
            release.set()
        await self.settled(first)

    async def test_atomic_delivery_race_rejects_old_version_transfer(self):
        run = self.complete(await self.new_run("conversation"))
        original = self.c.task_replies.request
        entered, release = asyncio.Event(), asyncio.Event()

        async def delayed(request):
            if request.action == "deliver":
                entered.set()
                await release.wait()
            return await original(request)

        self.runtime.handlers[ConversationFeedbackRequest] = ("conversation", delayed)
        delivery = asyncio.create_task(self.plugin.feedback.deliver(run, self.payload(run)))
        await entered.wait()
        takeover = asyncio.create_task(self.transfer(run, "background", version=0))
        release.set()
        await delivery
        with self.assertRaisesRegex(ValueError, "changed"):
            await takeover
        self.assertEqual(len(self.c.store.history("main")), 1)
        self.assertEqual(self.host.notifications.repo.list()["items"], [])

    async def test_projection_shares_execution_group_with_notification(self):
        run = self.complete(await self.new_run("conversation"))
        current = await self.settled(run)
        message = self.c.store.get(current["data"]["completion_feedback"]["message_id"])
        self.host.notifications.project_chat([message])
        from assistant_runtime.plugins.notifications.groups import association

        group, _ = association(
            "task:" + run["task_id"], "", {"title": "test", "origin": {"run_id": run["id"]}}
        )
        self.assertEqual(message["report_group"]["id"], group)
        self.assertEqual(message["report_group"]["task_id"], run["task_id"])

    async def test_different_results_in_same_voice_turn_have_distinct_output_ids(self):
        self.host.voice.opened = True
        self.host.backend.active_turn = "voice-turn"
        self.c.feedback.bind("voice-turn", "main", 1, 0, "voice")
        with patch.object(self.c, "speech_feedback", new_callable=AsyncMock) as speech:
            first = self.complete(await self.new_run("conversation"))
            second = self.complete(await self.new_run("conversation"))
            await self.settled(first)
            await self.settled(second)
        self.assertEqual(speech.await_count, 2)
        identities = {call.kwargs["delivery_id"] for call in speech.await_args_list}
        self.assertEqual(len(identities), 2)

    async def test_cancelled_speech_recovery_does_not_replay_audio(self):
        self.host.voice.opened = True
        self.host.backend.active_turn = "voice-turn"
        self.c.feedback.bind("voice-turn", "main", 1, 0, "voice")
        run = self.complete(await self.new_run("conversation"))
        self.host.voice.say = AsyncMock(side_effect=asyncio.CancelledError)
        with self.assertRaises(asyncio.CancelledError):
            await self.plugin.feedback.deliver(run, self.payload(run))
        self.c.feedback.recover("main")
        receipt = await self.plugin.feedback.deliver(run, self.payload(run))
        self.assertEqual(receipt["route"], "notification")
        self.host.voice.say.assert_awaited_once()
        self.assertEqual(len(self.host.notifications.repo.list()["items"]), 1)
