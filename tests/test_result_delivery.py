"""Received results survive transport/presentation failures without re-execution."""

import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from assistant_runtime.api.files import FileRequest
from assistant_runtime.api.results import ResultAccessError
from assistant_runtime.app.store import Store
from assistant_runtime.core.workfiles import WorkFiles


class ResultDeliveryTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "state.db")
        self.files = WorkFiles(self.root, self.store.db)
        self.results = self.files.results

    async def asyncTearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    async def put(self, text="hello", part="call", **kwargs):
        return await self.results.put("execution", part, "alice", {"text": text}, **kwargs)

    async def read(self, reference, **args):
        return await self.results.request(
            FileRequest("result_read", {"id": reference["id"], **args}), owner="alice"
        )

    async def test_large_result_paging_identity_grants_and_restart(self):
        text = "中文\n" * 25000 + "TAIL_ERROR operation:123"
        one, two = await asyncio.gather(self.put(text), self.put(text))
        self.assertEqual(one, two)
        self.assertTrue(one["complete_received"])
        self.assertIsNone(one["upstream_complete"])
        self.results = WorkFiles(self.root, self.store.db).results
        offset, parts = 0, []
        while True:
            reply = await self.read(one, offset=offset)
            self.assertLessEqual(len(reply["data"]), 6000)
            parts.append(reply["data"])
            offset = reply["next_offset"]
            if reply["eof"]:
                break
        self.assertEqual("".join(parts), text)
        with self.assertRaisesRegex(ResultAccessError, "forbidden"):
            self.results.row(one["id"], owner="bob")
        self.results.grant(one["id"], "bob")
        self.assertEqual(self.results.row(one["id"], owner="bob")["id"], one["id"])
        with self.assertRaisesRegex(ResultAccessError, "conflict"):
            await self.put("changed")
        with self.assertRaisesRegex(ResultAccessError, "version_conflict"):
            await self.read(one, version="wrong")

    async def test_retention_permanent_expiry_touch_holds_and_manual_conflicts(self):
        with patch("assistant_runtime.core.workfiles.results.time.time", return_value=1000):
            ref = await self.put(hold=False)
            held = await self.put(part="held")
        with patch("assistant_runtime.core.workfiles.results.time.time", return_value=400 * 86400):
            self.assertEqual((await self.results.collect())["deleted"], 0)
            self.results.days = 30
            await self.read(ref)
            self.assertEqual((await self.results.collect())["deleted"], 0)
            preview = await self.results.request(FileRequest("result_usage", {"ids": [ref["id"]]}))
            await self.read(ref)
            self.results.release("execution:execution")
            with self.assertRaisesRegex(ResultAccessError, "conflict"):
                await self.results.request(
                    FileRequest(
                        "result_collect", {"ids": [ref["id"]], "apply": True, "version": preview["version"]}
                    )
                )
            self.assertEqual((await self.results.collect())["deleted"], 1)
            self.assertEqual(self.results.row(held["id"], owner="alice")["state"], "expired")
        with patch("assistant_runtime.core.workfiles.results.time.time", return_value=431 * 86400):
            self.assertEqual((await self.results.collect())["deleted"], 1)
            with self.assertRaisesRegex(ResultAccessError, "expired"):
                await self.read(ref)

    async def test_quota_write_failure_corruption_and_no_broken_ready_receipts(self):
        self.results.quota = 100
        original = await self.put("a", hold=False)
        with self.assertRaisesRegex(ResultAccessError, "storage_full"):
            await self.put("x" * 101, part="large")
        self.assertEqual((await self.read(original))["data"], "a")
        self.results.quota = 1024 * 1024
        with patch.object(self.results, "write", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                await self.put("x" * 7000, part="disk")
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM received_results").fetchone()[0], 1)
        large = await self.put("z" * 7000, part="ok")
        (self.results.root / large["id"]).write_bytes(b"changed")
        with self.assertRaisesRegex(ResultAccessError, "corrupt"):
            await self.read(large)

    async def test_json_pointer_and_bounded_collection_read(self):
        value = {"text": "summary", "structured": {"a/b": [{"x": "v" * 20}] * 100}}
        ref = await self.results.put("execution", "json", "alice", value)
        chunk = await self.read(ref, format="json", path="/structured/a~1b", length=2)
        self.assertEqual(len(chunk["data"]), 2)
        self.assertFalse(chunk["eof"])
        chunk = await self.read(ref, format="text", path="/structured/a~1b/99/x", length=10)
        self.assertEqual(chunk["data"], "v" * 10)
        with self.assertRaisesRegex(ResultAccessError, "path_missing"):
            await self.read(ref, path="/not-found")
        with self.assertRaisesRegex(ResultAccessError, "invalid_range"):
            await self.read(ref, offset=-1)
        self.assertEqual(json.loads(self.results.content(self.results.row(ref["id"], owner="alice"))), value)

    async def test_defaults_and_all_retention_choices(self):
        self.assertEqual(self.results.days, 0)
        for days in (0, 7, 30, 365):
            self.assertEqual(
                WorkFiles(self.root, self.store.db, {"result_retention_days": days}).results.days, days
            )
        for days in (-1, True, 8, "30"):
            with self.assertRaises(ValueError):
                WorkFiles(self.root, self.store.db, {"result_retention_days": days})

    async def test_shared_images_expiry_and_owner_reconciliation(self):
        from test_tool_media import picture

        raw, image = picture()
        media = await self.files.tool_media.register(
            "execution", "capture", [image], "executions/execution", owner="alice"
        )
        attachment = media["attachments"][0]
        with patch("assistant_runtime.core.workfiles.results.time.time", return_value=100):
            first = await self.put("one", part="one", attachments=[attachment])
            second = await self.put("two", part="two", attachments=[attachment], hold=False)
        self.results.days = 7
        with patch("assistant_runtime.core.workfiles.results.time.time", return_value=10 * 86400):
            # One live reference keeps both original image and artifact available.
            self.assertEqual((await self.results.collect())["deleted"], 1)
            self.assertEqual(Path(attachment["path"]).read_bytes(), raw)
            self.assertEqual((self.files.media.folder / attachment["id"]).read_bytes(), raw)
            # The owner confirms a known terminal outcome. Missing owners never release holds.
            self.results.protection_checks["execution"] = lambda ident: False
            self.assertEqual((await self.results.collect())["deleted"], 1)
            self.assertFalse(Path(attachment["path"]).exists())
            self.assertFalse((self.files.media.folder / attachment["id"]).exists())
            for ref in (first, second):
                with self.assertRaisesRegex(ResultAccessError, "expired"):
                    await self.read(ref)

    async def test_deadlines_preview_does_not_touch_and_interrupted_write_recovery(self):
        for days in (7, 30, 365):
            with self.subTest(days=days):
                self.results.days = days
                with patch("assistant_runtime.core.workfiles.results.time.time", return_value=1000):
                    ref = await self.put(part=str(days), hold=False)
                with patch(
                    "assistant_runtime.core.workfiles.results.time.time", return_value=1000 + days * 86400 - 1
                ):
                    await self.results.request(FileRequest("result_get", {"id": ref["id"]}), owner="alice")
                    self.assertEqual((await self.results.collect())["deleted"], 0)
                with patch(
                    "assistant_runtime.core.workfiles.results.time.time", return_value=1000 + days * 86400
                ):
                    self.assertEqual((await self.results.collect())["deleted"], 1)
        self.results.root.mkdir(parents=True, exist_ok=True)
        orphan = self.results.root / ("f" * 32)
        orphan.write_bytes(b"interrupted write")
        unrelated = self.results.root / "user-file.txt"
        unrelated.write_text("keep")
        await self.results.collect()
        self.assertFalse(orphan.exists())
        self.assertTrue(unrelated.exists())
        ref = await self.put("z" * 8000, part="damaged", hold=False)
        (self.results.root / ref["id"]).unlink()
        with self.assertRaisesRegex(ResultAccessError, "missing"):
            await self.put("z" * 8000, part="damaged", hold=False)

    async def test_utf8_chunk_bounds_and_pending_notifications(self):
        from assistant_runtime.core.notifications.plugin import NotificationsPlugin
        from assistant_runtime.core.notifications.repository import Notifications
        from assistant_runtime.core.notifications.settings import NotificationSettings
        from types import SimpleNamespace

        ref = await self.put("汉" * 500000, hold=False)
        part = await self.read(ref, length=1048576)
        self.assertLessEqual(len(part["data"].encode()), 1048576)
        repo = Notifications(self.store.db, NotificationSettings.parse({}))
        item = repo.publish("test", "key", {"title": "result", "body": "ready"}, ["test"])
        plugin = SimpleNamespace(repo=repo)
        self.results.protection_checks["notification"] = lambda ident: NotificationsPlugin.result_protected(
            plugin, ident
        )
        self.results.hold(ref["id"], "notification:" + item["id"])
        with self.assertRaisesRegex(ResultAccessError, "protected"):
            await self.results.request(FileRequest("result_usage", {"ids": [ref["id"]]}))
        with self.store.db:
            self.store.db.execute("UPDATE notification_deliveries SET state='delivered'")
        preview = await self.results.request(FileRequest("result_usage", {"ids": [ref["id"]]}))
        self.assertEqual(preview["eligible_count"], 1)


class NativeResultDeliveryTest(unittest.IsolatedAsyncioTestCase):
    async def test_real_pi_large_tool_reader_and_long_final_across_entrances(self):
        from dataclasses import replace
        import sys
        import uuid
        from assistant_runtime.api import ExecutionRequest, ConversationRequest
        from assistant_runtime.api.contracts import TaskRequest
        from assistant_runtime.app.config import Config
        from assistant_runtime.app.initialize import initialize
        from assistant_runtime.app.host import AssistantService
        from pi_gateway import FakeResponsesGateway, sse_tool_call, sse_text_reply
        from test_pi_skeleton import configuration

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            count = root / "calls.txt"
            script = root / "sample.py"
            text = "HEAD " + "received data " * 1200 + " TAIL_ERROR operation:123"
            final = "FINAL " + "完整结论 " * 14000 + " FINAL_END"
            script.write_text(
                "from fastmcp import FastMCP\nfrom pathlib import Path\nm=FastMCP('sample')\n@m.tool()\ndef report():\n"
                f"    p=Path({str(count)!r}); p.write_text(p.read_text()+'x' if p.exists() else 'x')\n"
                f"    return {text!r}\n"
                "m.run(transport='stdio',show_banner=False)\n"
            )
            observed = []

            def model(step, body):
                phase = (step - 1) % 4
                if phase == 0:
                    return sse_tool_call(
                        "discover-" + str(step),
                        "tool_search",
                        {"query": "external_sample report", "limit": 1},
                    )
                if phase == 1:
                    return sse_tool_call("report-" + str(step), "mcp__external_sample__report", {})
                outputs = [i for i in body["input"] if i.get("type") == "function_call_output"]
                if phase == 2:
                    value = outputs[-1]["output"]
                    if isinstance(value, list):
                        value = "\n".join(i.get("text", "") for i in value)
                    receipt, _ = json.JSONDecoder().raw_decode(
                        value.split("[Framework result delivery] ", 1)[1]
                    )
                    observed.append((receipt, value))
                    self.assertIn("TAIL_ERROR", value)
                    self.assertLess(len(value), 9000)
                    self.assertTrue(any(t["name"] == "read_result" for t in body["tools"]))
                    return sse_tool_call(
                        "read-" + str(step),
                        "read_result",
                        {
                            "id": receipt["reference"]["id"],
                            "version": receipt["reference"]["version"],
                            "offset": len(text) - 24,
                            "length": 24,
                        },
                    )
                self.assertIn("operation:123", str(outputs[-1]))
                self.assertNotIn("Framework result delivery", str(outputs[-1]))
                return sse_text_reply("final-" + str(step), final)

            gateway = FakeResponsesGateway(model)
            config = replace(
                Config.load(initialize(root / "instance", preset="standard")),
                model_api=configuration(gateway.base_url),
                main_model="model-a",
                main_reasoning_effort="low",
                web={"enable": False},
            )
            (config.root / ".env").write_text("TEST_KEY=synthetic-key\n")
            host = AssistantService(config)
            host.mcp_registry.configure(
                "sample",
                {
                    "transport": {"type": "stdio", "command": sys.executable, "args": [str(script)]},
                    "contexts": ["foreground", "background"],
                    "approval": "approve",
                },
                0,
            )
            running = asyncio.create_task(host.run())
            policy = {"tools": ["mcp__external_sample__report", "read_result"], "skills": []}

            async def invoke(cls, action, args):
                return await host.runtime.invoke(cls(action, args))

            async def full(ref):
                chunks, offset = [], 0
                while True:
                    part = await invoke(
                        FileRequest,
                        "result_read",
                        {"id": ref["id"], "version": ref["version"], "offset": offset},
                    )
                    chunks.append(part["data"])
                    offset = part["next_offset"]
                    if part["eof"]:
                        return "".join(chunks)

            try:
                await asyncio.wait_for(host.runtime.ready.wait(), 20)
                # Shared execution is the slim CLI path; no second executor is involved.
                execution = await invoke(
                    ExecutionRequest,
                    "submit",
                    {
                        "request_id": str(uuid.uuid4()),
                        "text": "Synthetic report",
                        **policy,
                    },
                )
                async with asyncio.timeout(35):
                    while execution["state"] not in {"completed", "failed", "unknown"}:
                        await asyncio.sleep(0.03)
                        execution = await invoke(ExecutionRequest, "get", {"id": execution["id"]})
                self.assertEqual(execution["state"], "completed", execution)
                self.assertEqual(await full(execution["result_ref"]), final)
                self.assertLess(len(execution["result"]), 6100)
                session = (
                    await invoke(
                        ConversationRequest,
                        "create",
                        {"title": "Long report", "policy": {"tools": policy["tools"]}},
                    )
                )["id"]
                await invoke(
                    ConversationRequest,
                    "submit",
                    {"session_id": session, "request_id": str(uuid.uuid4()), "text": "Synthetic report"},
                )
                async with asyncio.timeout(35):
                    while (await invoke(ConversationRequest, "queue", {"session_id": session}))["requests"]:
                        await asyncio.sleep(0.03)
                history = await invoke(ConversationRequest, "history", {"session_id": session})
                refs = [m["result_ref"] for m in history["messages"] if m.get("result_ref")]
                self.assertEqual(await full(refs[-1]), final)
                excerpts = [
                    m for m in history["messages"] if m.get("presentation", {}).get("body") == "excerpt"
                ]
                self.assertEqual(len(excerpts), 1)
                self.assertEqual(excerpts[0]["presentation"]["result_offset"], 0)
                self.assertEqual(excerpts[0]["presentation"]["result_length"], len(final))
                self.assertEqual(excerpts[0]["presentation"]["turn_status"], "completed")
                task = await invoke(
                    TaskRequest,
                    "create",
                    {
                        "request_id": str(uuid.uuid4()),
                        "spec": {
                            "title": "Long report",
                            "prompt": "Synthetic report",
                            "notify_voice": False,
                        },
                    },
                )
                async with asyncio.timeout(40):
                    while True:
                        info = await invoke(TaskRequest, "get", {"task_id": task["id"]})
                        runs = info["runs"]
                        if runs and runs[0]["state"] in {"completed", "failed", "unknown"}:
                            break
                        await asyncio.sleep(0.05)
                self.assertEqual(runs[0]["state"], "completed", runs[0])
                self.assertEqual(await full(runs[0]["data"]["result_ref"]), final)
                self.assertTrue(runs[0]["data"]["result_is_preview"])
                self.assertEqual(count.read_text(), "xxx")
                self.assertEqual(len(gateway.requests), 12)
                self.assertEqual(len(observed), 3)
            finally:
                host.stop_event.set()
                await asyncio.wait_for(running, 15)
                await gateway.aclose()
