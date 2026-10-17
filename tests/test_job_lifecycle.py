"""Provider work directories, reversible archiving and conservative completion checks."""

import asyncio
import copy
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

from assistant_runtime.config import Config
from assistant_runtime.plugins.codex.jobs import CodexJobs


class JobLifecycleTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = Config(root=self.root, workspace=str(self.root))
        self.area = self.root / "run-area"
        for name in ("scratch", "artifacts"):
            (self.area / name).mkdir(parents=True)
        self.thread = {"id": "background", "path": "/sessions/job.jsonl", "status": {"type": "idle"}}
        self.turns = [{"id": "turn", "status": "completed"}]
        self.terminals = []
        self.goal = None
        self.lost_ack = False
        self.client = SimpleNamespace(request=AsyncMock(side_effect=self.rpc))
        self.backend = SimpleNamespace(
            client=self.client, config=self.config, thread_id="foreground", server_tasks=set()
        )
        self.notify = lambda *args, **kwargs: True
        self.runner = CodexJobs(self.backend, self.notify)
        self.runner.catalog = [
            {"id": "test", "model": "test", "supportedReasoningEfforts": [{"reasoningEffort": "low"}]}
        ]
        self.runner.catalog_at = time.monotonic()
        self.run = {
            "id": "run",
            "state": "completed",
            "data": {
                "thread_id": "background",
                "spec": {
                    "title": "authorized",
                    "prompt": "do the authorized work",
                    "workspace": str(self.root),
                    "execution": {"kind": "codex", "model": "test", "reasoning_effort": "low"},
                },
            },
        }

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def rpc(self, method, params, **kwargs):
        if method in {"thread/read", "thread/start", "thread/resume"}:
            return {"thread": {**copy.deepcopy(self.thread), "turns": copy.deepcopy(self.turns)}}
        if method == "thread/turns/list":
            return {"data": self.turns, "nextCursor": None}
        if method == "thread/backgroundTerminals/list":
            return {"data": self.terminals, "nextCursor": None}
        if method == "thread/goal/get":
            return {"goal": self.goal}
        if method == "thread/archive":
            self.thread["path"] = "/archived_sessions/job.jsonl"
            if self.lost_ack:
                self.lost_ack = False
                raise TimeoutError("archive committed, reply lost")
        if method == "thread/unarchive":
            self.thread["path"] = "/sessions/job.jsonl"
        if method == "turn/start":
            return {"turn": {"id": "turn"}}
        return {}

    def methods(self):
        return [call.args[0] for call in self.client.request.await_args_list]

    async def test_launch_uses_run_scratch_and_preserves_authorized_target(self):
        self.run["data"].pop("thread_id")
        await self.runner.launch(self.run, self.area)
        start = next(c.args[1] for c in self.client.request.await_args_list if c.args[0] == "thread/start")
        self.assertEqual(start["cwd"], str(self.area / "scratch"))
        self.assertEqual(
            start["config"]["shell_environment_policy.set"]["TMPDIR"], str(self.area / "scratch")
        )
        self.assertIn(str(self.area / "artifacts"), start["developerInstructions"])
        self.assertIn(self.config.workspace, start["developerInstructions"])
        self.assertEqual(start["sandbox"], self.config.codex_sandbox)
        self.assertNotIn("thread/archive", self.methods())

    async def test_background_mcp_configuration_and_revision_are_a_dispatch_snapshot(self):
        registry = SimpleNamespace(revision=7)
        capture = AsyncMock(return_value={"external_example": {"enabled": True}})
        self.backend.integrations = SimpleNamespace(registry=registry, capture=capture)
        self.run["data"]["spec"]["resources"] = ["mcp_management"]
        notices = []
        self.runner.notify = lambda *args, **kwargs: notices.append((args, kwargs))

        async def rpc(method, params, **kwargs):
            result = await self.rpc(method, params, **kwargs)
            if method == "thread/resume":
                registry.revision = 8
            return result

        self.client.request.side_effect = rpc
        await self.runner.launch(self.run, self.area)
        capture.assert_awaited_once_with(
            "background", "run", cwd=self.area / "scratch", manage_integrations=True
        )
        resume = next(c.args[1] for c in self.client.request.await_args_list if c.args[0] == "thread/resume")
        self.assertEqual(resume["config"]["mcp_servers"], {"external_example": {"enabled": True}})
        self.assertEqual(notices[0][1]["mcp_revision"], 7)
        self.assertEqual(self.runner.handles["run"]["mcp_revision"], 7)

    async def test_terminal_archive_releases_only_background_handles(self):
        self.runner.threads = {"background": "run", "other": "other-run"}
        self.runner.handles = {"run": {"thread_id": "background"}, "other-run": {}}
        self.runner.questions = {"run": {}, "other-run": {}}
        (self.area / "artifacts/result.txt").write_text("retain")
        await self.runner.finalize(self.run)
        self.assertIn("thread/archive", self.methods())
        self.assertIn("thread/unsubscribe", self.methods())
        self.assertEqual(self.runner.threads, {"other": "other-run"})
        self.assertNotIn("run", self.runner.handles)
        self.assertNotIn("run", self.runner.questions)
        self.assertTrue((self.area / "artifacts/result.txt").exists())
        page = next(
            c.args[1] for c in self.client.request.await_args_list if c.args[0] == "thread/turns/list"
        )
        self.assertEqual(page["limit"], 1)
        self.assertNotIn("turn/start", self.methods())

    async def test_lost_archive_ack_is_verified_without_reexecution(self):
        self.lost_ack = True
        await self.runner.finalize(self.run)
        self.assertEqual(self.methods().count("thread/archive"), 1)
        self.assertEqual(self.methods().count("thread/read"), 2)
        self.assertNotIn("turn/start", self.methods())
        self.client.request.reset_mock()
        await self.runner.finalize(self.run)
        self.assertNotIn("thread/archive", self.methods())
        self.assertNotIn("thread/unarchive", self.methods())

    async def test_explicit_continuation_unarchives_before_resume(self):
        self.thread["path"] = "/archived_sessions/job.jsonl"
        self.run["data"]["continuation"] = "continue only missing work"
        await self.runner.launch(self.run, self.area)
        methods = self.methods()
        self.assertLess(methods.index("thread/unarchive"), methods.index("thread/resume"))
        self.assertEqual(methods.count("turn/start"), 1)
        resume = next(c.args[1] for c in self.client.request.await_args_list if c.args[0] == "thread/resume")
        self.assertEqual(resume["cwd"], str(self.area / "scratch"))

    async def test_unfinished_execution_and_foreground_are_never_archived(self):
        for state in ("queued", "dispatching", "running", "waiting_user", "paused", "unknown"):
            with self.subTest(state=state), self.assertRaises(ValueError):
                await self.runner.finalize({**self.run, "state": state})
        self.run["data"]["thread_id"] = "foreground"
        with self.assertRaises(ValueError):
            await self.runner.finalize(self.run)
        self.assertEqual(self.methods(), [])

    async def test_provider_active_or_unfinished_turn_defers_archive(self):
        self.thread["status"] = {"type": "active"}
        with self.assertRaisesRegex(RuntimeError, "active"):
            await self.runner.finalize(self.run)
        self.thread["status"] = {"type": "idle"}
        self.turns[0]["status"] = "inProgress"
        with self.assertRaisesRegex(RuntimeError, "not ended"):
            await self.runner.finalize(self.run)
        self.assertNotIn("thread/archive", self.methods())

    async def test_live_background_command_defers_without_killing_it(self):
        self.terminals = [{"processId": "still-working"}]
        with self.assertRaisesRegex(RuntimeError, "commands"):
            await self.runner.finalize(self.run)
        self.assertNotIn("thread/archive", self.methods())
        self.assertNotIn("thread/backgroundTerminals/terminate", self.methods())

    async def test_canceled_empty_unpersisted_thread_only_unsubscribes(self):
        self.run["state"] = "canceled"
        self.thread["path"] = None
        self.turns = []
        await self.runner.finalize(self.run)
        self.assertNotIn("thread/archive", self.methods())
        self.assertIn("thread/unsubscribe", self.methods())

    async def test_one_shot_goal_is_paused_before_interrupt_on_cancel(self):
        self.run["data"].update(goal={"status": "active"}, turn_id="second")
        await self.runner.stop(self.run)
        self.assertLess(self.methods().index("thread/goal/set"), self.methods().index("turn/interrupt"))

    async def test_rejected_native_continuation_is_stopped_without_reopening_authority(self):
        self.runner.threads["background"] = "run"
        self.runner.handles["run"] = {"thread_id": "background", "goal": {"status": "active"}}
        self.runner.notify = Mock(return_value=False)
        await self.runner.event(
            {
                "method": "turn/started",
                "params": {
                    "threadId": "background",
                    "turn": {"id": "late"},
                },
            }
        )
        await asyncio.gather(*self.backend.server_tasks)
        self.assertIn("thread/goal/set", self.methods())
        stop = next(c.args[1] for c in self.client.request.await_args_list if c.args[0] == "turn/interrupt")
        self.assertEqual(stop, {"threadId": "background", "turnId": "late"})

    async def test_recovery_checks_actual_goal_instead_of_ending_one_shot_early(self):
        self.run["data"].update(goal={"status": "active"}, turn_id="turn")
        self.goal = {"status": "active"}
        state, _ = await self.runner.reconcile(self.run)
        self.assertEqual(state, "paused")
        self.assertNotIn("turn/start", self.methods())
        self.assertIn("thread/goal/set", self.methods())
        self.client.request.reset_mock()
        self.goal = {"status": "complete"}
        self.runner.notify = Mock()
        state, _ = await self.runner.reconcile(self.run)
        self.assertEqual(state, "completed")
        self.runner.notify.assert_called_once_with("run", None, goal=self.goal, turn_active=False)
        self.assertNotIn("thread/goal/set", self.methods())

    async def test_recovery_does_not_load_goal_thread_to_query_it(self):
        self.run["data"].update(goal={"status": "active"}, turn_id="turn")
        self.thread["status"] = {"type": "notLoaded"}
        state, _ = await self.runner.reconcile(self.run)
        self.assertEqual(state, "paused")
        self.assertEqual(self.methods(), ["thread/read"])

    async def test_multiple_background_capture_refs_are_retained_and_deduplicated(self):
        self.runner.threads["background"] = "run"
        self.runner.notify = Mock()
        first = {"id": "a" * 32, "name": "one.jpg", "type": "image/jpeg"}
        second = {"id": "b" * 32, "name": "two.jpg", "type": "image/jpeg"}
        for ref in (first, second, first):
            await self.runner.event(
                {
                    "method": "item/completed",
                    "params": {
                        "threadId": "background",
                        "item": {
                            "id": "capture",
                            "type": "mcpToolCall",
                            "status": "completed",
                            "tool": "capture_image",
                            "result": {"attachments": [ref]},
                        },
                    },
                }
            )
        self.runner.notify.assert_called_with("run", None, attachments=[first, second])

    async def test_rejected_cleanup_does_not_block_events_or_stop_newer_turn(self):
        self.runner.threads["background"] = "run"
        self.runner.handles["run"] = {"thread_id": "background"}
        self.runner.notify = Mock(return_value=False)
        entered, release = asyncio.Event(), asyncio.Event()

        async def hold(run, pause=False):
            entered.set()
            await release.wait()
            raise TimeoutError("private provider detail")

        self.runner._stop = hold
        await self.runner.event(
            {
                "method": "turn/started",
                "params": {
                    "threadId": "background",
                    "turn": {"id": "late"},
                },
            }
        )
        tasks = list(self.backend.server_tasks)
        await asyncio.wait_for(entered.wait(), 1)
        self.assertTrue(
            await self.runner.event(
                {
                    "method": "item/completed",
                    "params": {
                        "threadId": "background",
                        "item": {"type": "agentMessage", "text": "late output"},
                    },
                }
            )
        )
        release.set()
        with self.assertLogs("assistant_runtime.plugins.codex.jobs", level="WARNING") as logs:
            await asyncio.gather(*tasks)
        self.assertNotIn("private provider detail", "".join(logs.output))
        self.runner._stop = AsyncMock()
        self.runner.handles["run"]["turn_id"] = "explicit-new-turn"
        await self.runner.reject_continuation("run", "late")
        self.runner._stop.assert_not_awaited()
