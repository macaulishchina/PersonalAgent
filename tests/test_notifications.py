"""Durable notifications: idempotency, channel recovery, expiry and user read state."""

import asyncio
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from fastmcp import Client
from assistant_runtime.config import Config
from assistant_runtime.store import Store
from assistant_runtime.conversation.store import ConversationStore
from assistant_runtime.kernel.contracts import NotificationRequest
from assistant_runtime.plugins.notifications.plugin import NotificationsPlugin
from assistant_runtime.mcp.notifications import create_server
from assistant_runtime.mcp.catalog import NOTIFICATION_TOOLS


class NotificationsTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "state.sqlite")
        self.host = SimpleNamespace(
            config=Config(root=Path(self.tmp.name)),
            store=self.store,
            conversation=SimpleNamespace(store=ConversationStore(self.store), session="session-one"),
            backend=SimpleNamespace(busy=False, import_voice_messages=AsyncMock()),
            active=False,
            quiet_active=lambda: False,
            notify_task=AsyncMock(return_value="played"),
        )
        self.plugin = NotificationsPlugin(self.host)
        self.repo = self.plugin.repo

    async def asyncTearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    async def publish(self, **values):
        return await self.plugin.request(
            NotificationRequest(
                "publish",
                {
                    "event_key": "source-post-123",
                    "title": "新消息",
                    "body": "已核对的内容",
                    **values,
                },
            )
        )

    async def test_read_all_covers_pages_but_preserves_new_and_corrected_notices(self):
        now = time.time()
        with patch("assistant_runtime.plugins.notifications.repository.time.time", return_value=now):
            notices = [await self.publish(event_key=str(i)) for i in range(60)]
            snapshot = self.repo.list(limit=10)
        with patch("assistant_runtime.plugins.notifications.repository.time.time", return_value=now + 1):
            newer = await self.publish(event_key="new")
            corrected = await self.publish(id=notices[0]["id"], revision=1, body="更正")
        result = await self.plugin.request(
            NotificationRequest("read_all", {"before": snapshot["read_before"]})
        )
        self.assertEqual(result["marked_read"], 59)
        self.assertTrue(self.repo.get(newer["id"])["unread"])
        self.assertTrue(self.repo.get(corrected["id"])["unread"])
        self.assertEqual(self.repo.read_all(snapshot["read_before"])["marked_read"], 0)
        with self.assertRaises(ValueError):
            await self.plugin.request(
                NotificationRequest("read_all", {"before": snapshot["read_before"]}, "background")
            )
        for value in (None, float("nan"), float("inf"), True, time.time() + 100):
            with self.assertRaises(ValueError):
                self.repo.read_all(value)

    async def test_duplicate_event_has_one_notification_and_channel_jobs(self):
        a = await self.publish()
        b = await self.publish()
        self.assertEqual(a["id"], b["id"])
        self.assertEqual(len(self.repo.list()["items"]), 1)
        self.assertEqual(
            self.repo.db.execute("SELECT count(*) FROM notification_deliveries").fetchone()[0], 2
        )
        with self.assertRaisesRegex(ValueError, "different content"):
            await self.publish(body="changed")

    async def test_update_resets_read_and_stale_ack_does_not_read_correction(self):
        a = await self.publish()
        self.repo.mark_read(a["id"], 1)
        self.assertEqual(self.repo.unread_count(), 0)
        args = {"id": a["id"], "revision": 1, "title": "更正", "body": "更新的事实"}
        b = await self.plugin.request(NotificationRequest("update", args))
        self.assertEqual(b["revision"], 2)
        self.assertEqual(self.repo.unread_count(), 1)
        with self.assertRaisesRegex(ValueError, "changed"):
            self.repo.mark_read(a["id"], 1)
        retry = await self.plugin.request(NotificationRequest("update", args))
        self.assertEqual(retry["revision"], 2)

    async def test_channel_ack_is_not_read_and_chat_delivery_is_idempotent(self):
        a = await self.publish()
        channel = self.plugin.channels["chat"]
        await self.plugin.deliver_once("chat", channel)
        await channel.deliver(a)
        rows = self.store.db.execute("SELECT id FROM chat_messages").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(self.repo.unread_count(), 1)
        self.assertEqual(
            next(x["state"] for x in self.repo.get(a["id"])["deliveries"] if x["channel"] == "chat"),
            "delivered",
        )

    async def test_first_task_result_delivery_keeps_registered_media_without_prior_progress_card(self):
        refs = [{"id": "a" * 32}]
        resolved = [{"id": "a" * 32, "name": "capture.jpg", "type": "image/jpeg"}]
        self.host.runtime = SimpleNamespace(
            invoke=AsyncMock(return_value={"run-one": {"task_id": "task-one", "attachments": refs}})
        )
        self.host.conversation.media = SimpleNamespace(resolve=Mock(return_value=resolved))
        payload = {
            "title": "Task finished",
            "body": "Requested capture",
            "origin": {"task_id": "task-one", "run_id": "run-one"},
        }
        self.repo.publish("task:task-one", "task-result:run-one:0", payload, ["chat"])
        self.assertIsNone(self.host.conversation.store.get("run:run-one"))
        await self.plugin.deliver_once("chat", self.plugin.channels["chat"])
        self.assertEqual(self.host.conversation.store.get("run:run-one")["attachments"], resolved)
        self.host.conversation.media.resolve.assert_called_once_with(refs)

    async def test_receipt_survives_reopen_and_session_change(self):
        a = await self.publish()
        self.store.db.close()
        self.store = Store(Path(self.tmp.name) / "state.sqlite")
        self.host.store = self.store
        self.host.conversation = SimpleNamespace(store=ConversationStore(self.store), session="session-two")
        self.plugin = NotificationsPlugin(self.host)
        self.repo = self.plugin.repo
        self.assertEqual(self.repo.unread_count(), 1)
        await self.plugin.deliver_once("chat", self.plugin.channels["chat"])
        self.assertEqual(len(self.host.conversation.store.history("session-two")), 1)
        self.assertEqual(self.repo.get(a["id"])["payload"]["title"], "新消息")

    async def test_restart_replays_safe_channel_but_not_uncertain_voice(self):
        a = await self.publish(voice=True)
        self.repo.claim("chat")
        self.repo.claim("voice")
        self.repo.recover("chat", True)
        self.repo.recover("voice", False)
        states = {x["channel"]: x["state"] for x in self.repo.get(a["id"])["deliveries"]}
        self.assertEqual(states["chat"], "pending")
        self.assertEqual(states["voice"], "unknown")
        self.assertIsNone(self.repo.claim("voice"))
        self.repo.retry(a["id"], 1, "voice")
        self.assertIsNotNone(self.repo.claim("voice"))

    async def test_retry_backoff_and_failure_cap(self):
        a = await self.publish()
        channel = SimpleNamespace(replay_safe=True, deliver=AsyncMock(side_effect=OSError("offline")))
        self.plugin.settings.max_attempts = 2
        await self.plugin.deliver_once("chat", channel)
        self.assertIsNone(self.repo.claim("chat"))
        self.repo.db.execute("UPDATE notification_deliveries SET next_at=0 WHERE channel='chat'")
        self.repo.db.commit()
        await self.plugin.deliver_once("chat", channel)
        receipt = next(x for x in self.repo.get(a["id"])["deliveries"] if x["channel"] == "chat")
        self.assertEqual(receipt["state"], "failed")
        self.assertEqual(receipt["attempts"], 2)
        self.assertIsNone(self.repo.claim("chat"))

    async def test_quiet_period_defers_voice_without_blocking_chat_or_spending_attempts(self):
        a = await self.publish(voice=True)
        self.host.quiet_active = lambda: True
        await self.plugin.deliver_once("voice", self.plugin.channels["voice"])
        await self.plugin.deliver_once("chat", self.plugin.channels["chat"])
        receipts = {x["channel"]: x for x in self.repo.get(a["id"])["deliveries"]}
        self.assertEqual(receipts["voice"]["attempts"], 0)
        self.assertEqual(receipts["voice"]["state"], "pending")
        self.assertEqual(receipts["chat"]["state"], "delivered")
        self.host.notify_task.assert_not_awaited()

    async def test_expired_notice_remains_history_but_never_plays(self):
        a = await self.publish(voice=True, expires_at=time.time() - 1)
        self.assertEqual(self.repo.unread_count(), 0)
        self.assertIsNone(self.repo.claim("voice"))
        self.assertTrue(self.repo.get(a["id"])["expired"])
        self.assertEqual(len(self.repo.list()["items"]), 1)
        with self.assertRaises(ValueError):
            self.repo.retry(a["id"], 1, "voice")

    async def test_only_read_state_exists_and_read_does_not_cancel_delivery(self):
        a = await self.publish(voice=True)
        await self.plugin.request(NotificationRequest("get", {"id": a["id"]}))
        self.assertEqual(self.repo.unread_count(), 1)
        self.assertNotIn("archived", a)
        with self.assertRaisesRegex(ValueError, "Unknown notification action"):
            await self.plugin.request(NotificationRequest("archive", {"id": a["id"], "revision": 1}))
        await self.plugin.request(NotificationRequest("read", {"id": a["id"], "revision": 1}))
        self.assertEqual(self.repo.unread_count(), 0)
        self.assertEqual(len(self.repo.list()["items"]), 1)
        self.assertFalse(self.repo.list()["items"][0]["unread"])
        self.assertIsNotNone(self.repo.claim("chat"))
        self.assertIsNotNone(self.repo.claim("voice"))

    async def test_removing_archive_preserves_history_receipts_and_corrections(self):
        a = await self.publish(event_key="old-dismissed")
        unread = await self.publish(event_key="still-unread")
        with self.repo.db:
            self.repo.db.execute("ALTER TABLE notifications ADD COLUMN archived INTEGER NOT NULL DEFAULT 0")
            self.repo.db.execute("UPDATE notifications SET archived=1 WHERE id=?", (a["id"],))
            self.repo.db.execute(
                "UPDATE notification_deliveries SET state='dismissed' WHERE notification_id=?", (a["id"],)
            )
        before_jobs = self.repo.db.execute(
            "SELECT * FROM notification_deliveries ORDER BY notification_id,channel"
        ).fetchall()
        self.plugin = NotificationsPlugin(self.host)
        self.repo = self.plugin.repo
        restored = self.repo.get(a["id"])
        self.assertFalse(restored["unread"])
        self.assertEqual(restored["revision"], a["revision"])
        self.assertEqual(restored["payload"], a["payload"])
        self.assertEqual(restored["updated"], a["updated"])
        self.assertEqual(self.repo.unread_count(), 1)
        self.assertTrue(self.repo.get(unread["id"])["unread"])
        self.assertEqual({item["id"] for item in self.repo.list()["items"]}, {a["id"], unread["id"]})
        self.assertNotIn("archived", {r[1] for r in self.repo.db.execute("PRAGMA table_info(notifications)")})
        self.assertEqual(
            before_jobs,
            self.repo.db.execute(
                "SELECT * FROM notification_deliveries ORDER BY notification_id,channel"
            ).fetchall(),
        )
        # A second startup is idempotent and does not reset later unread corrections.
        corrected = await self.publish(id=a["id"], revision=1, body="已更正")
        self.assertEqual(corrected["revision"], 2)
        self.assertTrue(corrected["unread"])
        self.plugin = NotificationsPlugin(self.host)
        self.repo = self.plugin.repo
        self.assertTrue(self.repo.get(a["id"])["unread"])
        with self.assertRaises(ValueError):
            self.repo.mark_read(a["id"], 1)

    async def test_background_cannot_mark_read_and_events_dedupe_across_runs(self):
        import json

        self.repo.db.execute("CREATE TABLE job_runs(id TEXT PRIMARY KEY, task_id TEXT, data TEXT)")
        for run in ("a", "b"):
            self.repo.db.execute(
                "INSERT INTO job_runs VALUES (?,?,?)",
                (run, "monitor", json.dumps({"spec": {"title": "监控"}})),
            )
        self.repo.db.commit()
        args = {"event_key": "post123", "title": "预告", "body": "有来源的预告"}
        a = await self.plugin.request(NotificationRequest("publish", args, "a"))
        b = await self.plugin.request(NotificationRequest("publish", args, "b"))
        self.assertEqual(a["id"], b["id"])
        self.assertEqual(b["source"], "task:monitor")
        with self.assertRaises(ValueError):
            await self.plugin.request(NotificationRequest("read", {"id": a["id"], "revision": 1}, "b"))

    async def test_cancelled_delivery_keeps_uncertainty_for_restart(self):
        await self.publish(voice=True)
        started = asyncio.Event()

        async def deliver(item):
            started.set()
            await asyncio.Event().wait()

        task = asyncio.create_task(
            self.plugin.deliver_once("voice", SimpleNamespace(replay_safe=False, deliver=deliver))
        )
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.repo.recover("voice", False)
        self.assertIsNone(self.repo.claim("voice"))

    async def test_new_channel_contract_and_mcp_are_independent(self):
        adapter = SimpleNamespace(replay_safe=True, deliver=AsyncMock())
        self.plugin.register_channel("future_push", adapter)
        with self.assertRaises(ValueError):
            self.plugin.register_channel("future_push", adapter)
        client = SimpleNamespace(invoke=AsyncMock(return_value={}))
        async with Client(create_server(client, "run1")) as mcp:
            names = {x.name for x in await mcp.list_tools()}
            self.assertEqual(names, set(NOTIFICATION_TOOLS))
            self.assertNotIn("archive_notification", names)
            self.assertIn("update_notification", names)
            await mcp.call_tool(
                "publish_notification", {"event_key": "event", "title": "title", "body": "body"}
            )
        request = client.invoke.await_args.args[0]
        self.assertEqual(request.caller_run, "run1")
        self.assertEqual(request.action, "publish")

    async def test_invalid_source_and_bounded_queue(self):
        with self.assertRaises(ValueError):
            await self.publish(url="javascript:alert(1)")
        with self.assertRaises(ValueError):
            await self.publish(expires_at=float("nan"))
        await self.publish()
        self.plugin.settings.max_notifications = 1
        with self.assertRaisesRegex(ValueError, "capacity"):
            await self.publish(event_key="another")
        self.assertEqual(len(self.repo.list()["items"]), 1)
