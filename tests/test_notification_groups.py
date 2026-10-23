"""Generic grouping: identity boundaries, migration, paging and concurrent acknowledgements."""

import json
import time
import unittest
from unittest.mock import patch

from assistant_runtime.api.contracts import NotificationLink, NotificationRequest
from assistant_runtime.core.notifications.repository import Notifications
from assistant_runtime.plugins.media.notifications import publish as publish_media, restore_associations
from types import SimpleNamespace

import test_notifications as base


class GroupTest(unittest.IsolatedAsyncioTestCase):
    asyncTearDown = base.NotificationsTest.asyncTearDown

    async def asyncSetUp(self):
        await base.NotificationsTest.asyncSetUp(self)
        self.repo.db.execute("CREATE TABLE job_runs(id TEXT PRIMARY KEY,task_id TEXT,data TEXT)")
        for run, task in (("one", "task-a"), ("two", "task-a"), ("other", "task-b")):
            self.repo.db.execute(
                "INSERT INTO job_runs VALUES(?,?,?)", (run, task, json.dumps({"spec": {"title": "同名任务"}}))
            )
        self.repo.db.commit()

    async def notice(self, key, run=None, **kw):
        return await self.plugin.request(
            NotificationRequest("publish", {"event_key": key, "title": "相同标题", "body": "内容", **kw}, run)
        )

    async def test_parent_report_coverage_never_silences_event_only_monitors(self):
        covered = await self.notice("operation-one", "one", kind="result", report_with_task=True)
        self.assertEqual(covered["payload"]["kind"], "progress")
        self.assertFalse(covered["unread"])
        self.assertEqual(covered["deliveries"], [])
        with self.repo.db:
            self.repo.db.execute(
                "UPDATE job_runs SET data=? WHERE id='two'",
                (json.dumps({"spec": {"title": "同名任务", "notify_completion": False}}),),
            )
        monitor = await self.notice("operation-two", "two", kind="result", report_with_task=True)
        self.assertEqual(monitor["payload"]["kind"], "result")
        self.assertTrue(monitor["unread"])
        standalone = await self.notice("standalone", kind="result", report_with_task=True)
        self.assertEqual(standalone["payload"]["kind"], "result")
        repeated = await self.notice("operation-one", "two", kind="result", report_with_task=True)
        self.assertEqual(repeated["id"], covered["id"])
        self.assertEqual(repeated["payload"]["kind"], "progress")
        self.assertEqual(repeated["revision"], 1)
        quiet = await self.notice("quiet", "two", kind="progress")
        self.assertFalse(quiet["unread"])
        with self.repo.db:
            self.repo.db.execute(
                "UPDATE job_runs SET data=? WHERE id='one'",
                (json.dumps({"spec": {"title": "同名任务"}, "completion_delivered": True}),),
            )
        late = await self.notice("late-result", "one", kind="result", report_with_task=True)
        self.assertEqual(late["payload"]["kind"], "result")
        self.assertTrue(late["unread"])
        retry = await self.notice("operation-one", "one", kind="result", report_with_task=True)
        self.assertEqual(retry["id"], covered["id"])
        self.assertFalse(retry["unread"])

    async def test_result_after_execution_ends_is_not_hidden_by_pending_final_report(self):
        covered = await self.notice("before-finish", "one", kind="result", report_with_task=True)
        with self.repo.db:
            self.repo.db.execute(
                "UPDATE job_runs SET data=? WHERE id='one'",
                (
                    json.dumps(
                        {
                            "spec": {"title": "同名任务"},
                            "finished_at": time.time(),
                            "completion_delivered": False,
                        }
                    ),
                ),
            )
        late = await self.notice("after-finish", "one", kind="result", report_with_task=True)
        self.assertEqual(late["payload"]["kind"], "result")
        self.assertTrue(late["unread"])
        self.assertTrue(late["deliveries"])
        self.assertEqual(late["group_id"], covered["group_id"])
        retry = await self.notice("before-finish", "one", kind="result", report_with_task=True)
        self.assertEqual(retry["id"], covered["id"])
        self.assertEqual(retry["deliveries"], [])
        self.assertEqual(retry["revision"], 1)

    async def test_quiet_progress_cannot_bury_result_or_actionable_reminder(self):
        result = await self.notice("result", "one", kind="result")
        await self.notice("progress", "one", kind="progress")
        other = await self.notice("other", "two", kind="progress")
        groups = self.repo.groups.list(unread=True)
        self.assertEqual(len(groups["items"]), 1)
        self.assertEqual(groups["items"][0]["latest"]["id"], result["id"])
        self.assertEqual(groups["items"][0]["unread"], 1)
        self.assertEqual(groups["items"][0]["total"], 2)
        attention = await self.notice("attention", "one", kind="attention", priority="important")
        await self.notice("late-progress", "one", kind="progress")
        group = self.repo.groups.list(unread=True)["items"][0]
        self.assertEqual(group["latest"]["id"], attention["id"])
        self.assertEqual(self.repo.groups.read(group["id"], group["read_before"])["marked_read"], 2)
        by_id = {g["id"]: g for g in self.repo.groups.list()["items"]}
        self.assertEqual(by_id[group["id"]]["latest"]["id"], result["id"])
        self.assertEqual(by_id[other["group_id"]]["unread"], 0)
        self.assertEqual(len(self.repo.groups.members(group["id"])["items"]), 4)

    async def test_explicit_association_scope_and_cross_run_event_deduplication(self):
        a = await self.notice("first", "one")
        b = await self.notice("second", "one")
        c = await self.notice("third", "two")
        d = await self.notice("fourth", "other")
        self.assertEqual(a["group_id"], b["group_id"])
        self.assertEqual(len({a["group_id"], c["group_id"], d["group_id"]}), 3)
        repeated = await self.notice("first", "two")
        self.assertEqual(repeated["group_id"], a["group_id"])
        x = await self.notice("topic1", "one", group_key="account-event", group_title="账号事件")
        y = await self.notice("topic2", "two", group_key="account-event", group_title="账号事件")
        z = await self.notice("topic3", "other", group_key="account-event", group_title="账号事件")
        self.assertEqual(x["group_id"], y["group_id"])
        self.assertNotEqual(x["group_id"], z["group_id"])
        p = await self.notice("foreground-a")
        q = await self.notice("foreground-b")
        self.assertNotEqual(p["group_id"], q["group_id"])
        for args in ({"group_key": ""}, {"group_title": "no key"}, {"group_key": 7}):
            with self.assertRaises(ValueError):
                await self.notice("invalid", **args)
        with self.assertRaises(ValueError):
            await self.plugin.request(
                NotificationRequest(
                    "update",
                    {"id": x["id"], "revision": 1, "title": "t", "body": "b", "group_key": "another"},
                    "one",
                )
            )

    async def test_chat_projection_reuses_associations_without_rewriting_history(self):
        first = await self.notice("first", "one")
        second = await self.notice("second", "one", priority="important")
        other = await self.notice("other", "two")
        for notice in (first, second, other):
            await self.plugin.channels["chat"].deliver(notice)
        chat = self.host.conversation.store
        raw = chat.history("session-one")
        cursor, unread = chat.cursor(), self.repo.unread_count()
        messages = chat.history("session-one")
        statements = []
        self.repo.db.set_trace_callback(statements.append)
        try:
            self.plugin.project_chat(messages)
        finally:
            self.repo.db.set_trace_callback(None)
        self.assertEqual(len(statements), 1)
        self.assertEqual(messages[0]["report_group"]["id"], messages[1]["report_group"]["id"])
        self.assertNotEqual(messages[0]["report_group"]["id"], messages[2]["report_group"]["id"])
        self.assertTrue(messages[1]["report_group"]["important"])
        self.assertEqual(messages[0]["report_group"]["run_id"], "one")
        self.assertEqual(messages[0]["task_id"], "task-a")
        self.assertEqual(chat.history("session-one"), raw)
        self.assertEqual((chat.cursor(), self.repo.unread_count()), (cursor, unread))

    async def test_chat_projection_handles_missing_and_restored_associations(self):
        notice = await self.notice("legacy")
        await self.plugin.channels["chat"].deliver(notice)
        chat = self.host.conversation.store
        original = chat.history("session-one")
        await self.plugin.link(NotificationLink("assistant", "legacy", "one"))
        messages = chat.history("session-one")
        messages += [
            {"role": "assistant", "kind": "notification", "turn_id": "notification:missing"},
            {"role": "user", "kind": "notification", "turn_id": "notification:" + notice["id"]},
            {"role": "assistant", "kind": "notification", "turn_id": "notification:bad:id"},
        ]
        self.plugin.project_chat(messages)
        self.assertEqual(messages[0]["report_group"]["id"], self.repo.get(notice["id"])["group_id"])
        # Legacy association repair deliberately preserves the original payload;
        # an opaque group identity cannot fabricate a missing task/run link.
        self.assertIsNone(messages[0]["task_id"])
        self.assertIsNone(messages[0]["report_group"]["run_id"])
        self.assertTrue(all("report_group" not in m for m in messages[1:]))
        self.assertEqual(chat.history("session-one"), original)

    async def test_group_pages_and_member_pages_do_not_depend_on_loaded_items(self):
        stamp = time.time() - 10
        with patch("time.time", return_value=stamp):
            group_a = [await self.notice(str(i), "one") for i in range(55)]
            await self.notice("other", "two")
            await self.notice("third", "other", priority="important")
            first = self.repo.groups.list(limit=1)
        # New activity is left for refresh and cannot shift an unseen group past the cursor.
        await self.notice("arrived later", "one")
        pages = first["items"][:]
        before = first["before"]
        while before:
            page = self.repo.groups.list(limit=1, before=before)
            pages += page["items"]
            before = page["before"]
        self.assertEqual(len(pages), 3)
        self.assertEqual(len({p["id"] for p in pages}), 3)
        self.assertEqual(next(g for g in pages if g["id"] == group_a[0]["group_id"])["total"], 55)
        group_id = group_a[0]["group_id"]
        bounded = self.repo.groups.members(group_id, snapshot=first["read_before"], limit=50)
        self.assertNotIn("arrived later", [i["event_key"] for i in bounded["items"]])
        page = self.repo.groups.members(group_id, limit=10)
        ids = [i["id"] for i in page["items"]]
        while page["before"]:
            page = self.repo.groups.members(group_id, before=page["before"], limit=10)
            ids += [i["id"] for i in page["items"]]
        self.assertEqual(len(ids), 56)
        self.assertEqual(len(set(ids)), 56)
        with self.assertRaises(ValueError):
            self.repo.groups.list(unread=True, before=first["before"])
        with self.assertRaises(ValueError):
            self.repo.groups.members(group_id, before=first["before"])
        with self.assertRaises(ValueError):
            self.repo.groups.list(limit=51)

    async def test_group_read_preserves_arrivals_corrections_and_other_groups(self):
        stamp = time.time() - 10
        with patch("time.time", return_value=stamp):
            a = await self.notice("a", "one")
            b = await self.notice("b", "one")
            c = await self.notice("c", "two")
            snapshot = self.repo.groups.list(unread=True)
        newer = await self.notice("new", "one")
        await self.plugin.request(
            NotificationRequest(
                "update", {"id": a["id"], "revision": 1, "title": "修正", "body": "新内容"}, "one"
            )
        )
        args = {"group_id": a["group_id"], "before": snapshot["read_before"]}
        with self.assertRaises(ValueError):
            await self.plugin.request(NotificationRequest("read_group", args, "one"))
        result = await self.plugin.request(NotificationRequest("read_group", args))
        self.assertEqual(result["marked_read"], 1)
        self.assertFalse(self.repo.get(b["id"])["unread"])
        self.assertTrue(all(self.repo.get(i["id"])["unread"] for i in (a, c, newer)))
        groups = self.repo.groups.list(unread=True)["items"]
        self.assertEqual(next(g for g in groups if g["id"] == a["group_id"])["unread"], 2)
        self.assertEqual(self.repo.groups.read(a["group_id"], snapshot["read_before"])["marked_read"], 0)
        self.assertEqual(len(self.repo.groups.members(a["group_id"])["items"]), 3)

    async def test_migration_and_trusted_links_preserve_content_read_and_delivery(self):
        a = await self.notice("legacy")
        await self.plugin.request(NotificationRequest("read", {"id": a["id"], "revision": 1}))
        jobs = self.repo.db.execute("SELECT * FROM notification_deliveries").fetchall()
        with self.repo.db:
            self.repo.db.execute("DROP INDEX notification_group_page")
            self.repo.db.execute("ALTER TABLE notifications DROP COLUMN group_id")
            self.repo.db.execute("ALTER TABLE notifications DROP COLUMN group_title")
        self.plugin.repo = self.repo = Notifications(self.repo.db, self.plugin.settings)
        old = self.repo.get(a["id"])
        linked = await self.plugin.link(NotificationLink("assistant", "legacy", "one"))
        self.assertTrue(linked["linked"])
        linked_again = await self.plugin.link(NotificationLink("assistant", "legacy", "one"))
        self.assertEqual(linked, linked_again)
        new = self.repo.get(a["id"])
        self.assertEqual(
            {k: v for k, v in new.items() if not k.startswith("group_")},
            {k: v for k, v in old.items() if not k.startswith("group_")},
        )
        self.assertEqual(new["group_title"], "同名任务")
        self.assertEqual(jobs, self.repo.db.execute("SELECT * FROM notification_deliveries").fetchall())
        task_notice = await self.notice("task-result", "one")
        self.assertEqual(new["group_id"], task_notice["group_id"])
        with self.assertRaises(ValueError):
            await self.plugin.link(NotificationLink("assistant", "legacy", "two"))
        with self.assertRaises(ValueError):
            await self.plugin.request(NotificationRequest("link", {"source": "assistant"}))

    async def test_media_adapter_uses_durable_owner_and_does_not_duplicate_lost_ack(self):
        self.repo.db.execute("CREATE TABLE media_operations(id TEXT PRIMARY KEY,data TEXT)")
        self.repo.db.execute("CREATE TABLE media_batches(id TEXT PRIMARY KEY,data TEXT)")
        self.repo.db.execute(
            "INSERT INTO media_operations VALUES('op',?)", (json.dumps({"caller_run": "one"}),)
        )
        self.repo.db.commit()

        async def invoke(request):
            return await (
                self.plugin.link(request)
                if isinstance(request, NotificationLink)
                else self.plugin.request(request)
            )

        ctx = SimpleNamespace(get=lambda *args: False, set=lambda *args: None)
        p = SimpleNamespace(
            repo=SimpleNamespace(db=self.repo.db),
            api=SimpleNamespace(invoke=invoke),
            context=ctx,
        )
        args = {"event_key": "media-complete:op", "title": "已完成", "body": "操作核验"}
        old = await self.plugin.request(NotificationRequest("publish", args))
        await restore_associations(p)
        await publish_media(p, args)
        self.assertEqual(self.repo.db.execute("SELECT count(*) FROM notifications").fetchone()[0], 1)
        task = await self.notice("summary", "one")
        self.assertEqual(self.repo.get(old["id"])["group_id"], task["group_id"])
        args["event_key"] = "media-attention:op"
        await publish_media(p, args)
        items = self.repo.groups.members(task["group_id"])["items"]
        self.assertEqual(len(items), 3)
        self.assertIn("task:task-a", {i["source"] for i in items})
