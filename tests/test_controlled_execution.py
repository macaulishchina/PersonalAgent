"""Real Pi retained contexts use only fenced, host-bound resource operations."""

import asyncio
import os
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
import uuid

from assistant_runtime.app.config import Config
from assistant_runtime.app.store import Store
from assistant_runtime.core.ai_usage.service import UsageService
from assistant_runtime.core.execution.service import ExecutionService
from assistant_runtime.core.pi.plugin import PiPlugin
from assistant_runtime.core.workfiles import WorkFiles
from assistant_runtime.kernel.runtime import Manifest, Runtime
from pi_gateway import FakeResponsesGateway, sse_text_reply, sse_tool_call
from test_pi_skeleton import configuration


@unittest.skipUnless(os.name == "nt", "Windows controlled backend")
class ControlledIntegrationTest(unittest.IsolatedAsyncioTestCase):
    async def test_retained_context_tools_and_revocation(self):
        import threading

        release_unknown = threading.Event()

        def reply(step, body):
            if step == 1:
                return sse_tool_call(
                    "write",
                    "pa_write",
                    dict(area="work", path="result.txt", text="ROUND_ONE", expected_sha256=None),
                )
            if step == 2:
                self.assertIn("completed", str(body))
                return sse_tool_call(
                    "artifact",
                    "pa_write",
                    dict(area="artifacts", path="result.txt", text="IMMUTABLE", expected_sha256=None),
                )
            if step == 3:
                return sse_tool_call("forbidden", "bash", {"command": "echo ESCAPED > ../escape.txt"})
            if step == 4:
                self.assertIn("Tool bash not found", str(body))
                return sse_text_reply("done", "FIRST_CONTEXT_MARKER")
            if step == 7:
                release_unknown.wait(15)
                return sse_tool_call(
                    "late-write",
                    "pa_write",
                    dict(area="work", path="late.txt", text="MUST_NOT_WRITE", expected_sha256=None),
                )
            if step == 8:
                self.assertNotIn("FIRST_CONTEXT_MARKER", str(body))
                return sse_text_reply("fresh", "FRESH_RETRY")
            self.assertIn("FIRST_CONTEXT_MARKER", str(body))
            return sse_text_reply("done", "SECOND_CONTEXT_MARKER")

        gateway = FakeResponsesGateway(reply)
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / ".env").write_text("TEST_KEY=synthetic-key\n")
            config = Config(
                root=root,
                workspace=str(root),
                plugins=["ai_usage", "models", "pi"],
                model_api=configuration(gateway.base_url),
            )
            store = Store(root / "execution.sqlite3")
            usage = UsageService(root / "usage")
            pi = PiPlugin(config, store, usage=usage, foreground=False)
            service = ExecutionService(
                pi.backend.client,
                store.db,
                WorkFiles(root, store.db),
                usage,
                default_model="model-a",
                default_effort="low",
                servers=lambda: {},
                controllers={"bridge": ["owner"]},
            )
            runtime = Runtime(store)

            async def start_usage(context):
                context.own(usage.close)
                await usage.start()

            runtime.add(SimpleNamespace(manifest=Manifest("ai_usage"), start=start_usage))
            runtime.add(pi)
            runtime.add(SimpleNamespace(manifest=Manifest("execution", ("pi",)), start=service.start))
            pi.backend.executions = service

            async def control(action, **args):
                return await service.control("bridge", action, args)

            async def settled(ident):
                async with asyncio.timeout(30):
                    while service.contexts.get("context", context)["data"]["state"] == "active":
                        await asyncio.sleep(0.01)
                result = service.view(service.records.get(ident))
                self.assertEqual(result["state"], "completed", result)
                return result

            try:
                await runtime.start()
                workspace, context, grant, first, second = [str(uuid.uuid4()) for _ in range(5)]
                await control(
                    "workspace_create",
                    workspace_id=workspace,
                    owner="owner",
                    version="v1",
                    usage_scope_id="analysis",
                    expires_at=time.time() + 600,
                )
                binding = dict(
                    owner="owner",
                    actor="member",
                    context_id=context,
                    workspace_id=workspace,
                    workspace_version="v1",
                    usage_scope_id="analysis",
                )
                await control(
                    "grant_issue",
                    grant_id=grant,
                    binding=binding,
                    actions=["execute", "read", "edit", "publish"],
                    expires_at=time.time() + 60,
                )
                await control(
                    "context_create",
                    context_id=context,
                    grant_id=grant,
                    expires_at=time.time() + 300,
                    model="model-a",
                    effort="low",
                )
                args = dict(
                    context_id=context, request_id=first, revision=1, text="Write the first round marker"
                )
                await control("context_run", **args)
                result = await settled(first)
                self.assertIn("FIRST_CONTEXT_MARKER", result["result"])
                self.assertFalse(list(root.rglob("escape.txt")))
                self.assertEqual(
                    (service.broker.files.root(workspace) / "work/result.txt").read_text(), "ROUND_ONE"
                )
                self.assertEqual((await control("context_run", **args))["id"], first)
                from assistant_runtime.api.files import FileRequest
                from assistant_runtime.api.runs import ExecutionRequest

                with self.assertRaises(PermissionError):
                    await service.request(ExecutionRequest("get", {"id": first}))
                finalize = dict(
                    finalize_id=str(uuid.uuid4()),
                    execution_id=first,
                    name="result.txt",
                    expires_at=time.time() + 600,
                )
                artifact = await control("artifact_finalize", grant_id=grant, **finalize)
                self.assertTrue(artifact["readable"])
                self.assertFalse(artifact["archived"])
                self.assertEqual(
                    (await control("artifact_finalize", grant_id=grant, **finalize))["id"], artifact["id"]
                )
                with self.assertRaises(PermissionError):
                    await service.files.publications.request(
                        FileRequest("artifact_read", {"id": artifact["id"]})
                    )
                with self.assertRaises(ValueError):
                    await control(
                        "artifact_acknowledge",
                        grant_id=grant,
                        id=artifact["id"],
                        sha256="wrong",
                        bytes=9,
                        archive_reference="external/test",
                    )
                receipt = await control(
                    "artifact_acknowledge",
                    grant_id=grant,
                    id=artifact["id"],
                    sha256=artifact["sha256"],
                    bytes=artifact["bytes"],
                    archive_reference="external/test",
                )
                self.assertTrue(receipt["archived"])
                first_session = service.contexts.get("context", context)["data"]["native_session"]
                revision = (await control("context_get", id=context))["revision"]
                await control(
                    "context_continue",
                    context_id=context,
                    request_id=second,
                    revision=revision,
                    previous_execution_id=first,
                    text="Recall the previous result",
                )
                result = await settled(second)
                self.assertIn("SECOND_CONTEXT_MARKER", result["result"])
                self.assertEqual(
                    service.contexts.get("context", context)["data"]["native_session"], first_session
                )
                a, b = await usage.binding(first), await usage.binding(second)
                self.assertEqual(a.work_id, b.work_id)
                self.assertNotEqual(a.caller, b.caller)
                self.assertEqual(len(gateway.requests), 5)
                import base64

                source = service.broker.files.area(workspace, "artifacts", first) / "result.txt"
                source.write_text("CHANGED")
                content = await control("artifact_read", grant_id=grant, id=artifact["id"])
                self.assertEqual(base64.b64decode(content["data"]), b"IMMUTABLE")
                evidence = await control("execution_reconcile", id=first)
                self.assertTrue(evidence["safe_to_retry"])
                self.assertEqual(evidence["local_writes"], "fenced")
                # A clean host restart retains native context but no live authority.
                await runtime.close()
                store.db.close()
                store = Store(root / "execution.sqlite3")
                usage = UsageService(root / "usage")
                pi = PiPlugin(config, store, usage=usage, foreground=False)
                service = ExecutionService(
                    pi.backend.client,
                    store.db,
                    WorkFiles(root, store.db),
                    usage,
                    default_model="model-a",
                    default_effort="low",
                    servers=lambda: {},
                    controllers={"bridge": ["owner"]},
                )
                runtime = Runtime(store)
                runtime.add(SimpleNamespace(manifest=Manifest("ai_usage"), start=start_usage))
                runtime.add(pi)
                runtime.add(SimpleNamespace(manifest=Manifest("execution", ("pi",)), start=service.start))
                pi.backend.executions = service
                await runtime.start()
                third = str(uuid.uuid4())
                revision = (await control("context_get", id=context))["revision"]
                continuation = dict(
                    context_id=context,
                    request_id=third,
                    revision=revision,
                    previous_execution_id=second,
                    text="Continue after host restart",
                )
                with self.assertRaises(PermissionError):
                    await control("context_continue", **continuation)
                self.assertEqual(len(gateway.requests), 5)
                await control("grant_renew", grant_id=grant, generation=1, expires_at=time.time() + 60)
                await control("context_continue", **continuation)
                await settled(third)
                self.assertEqual(
                    service.contexts.get("context", context)["data"]["native_session"], first_session
                )
                self.assertEqual((await usage.snapshot(a.work_id))["requests"], 6)
                fourth = str(uuid.uuid4())
                revision = (await control("context_get", id=context))["revision"]
                await control(
                    "context_continue",
                    context_id=context,
                    request_id=fourth,
                    revision=revision,
                    previous_execution_id=third,
                    text="Simulate a disconnected remote result",
                )
                async with asyncio.timeout(10):
                    while len(gateway.requests) < 7:
                        await asyncio.sleep(0.01)
                await control("grant_revoke", grant_id=grant)
                release_unknown.set()
                await asyncio.wait_for(service.queue.join(), 20)
                self.assertEqual(service.records.get(fourth)["state"], "unknown")
                self.assertFalse((service.broker.files.root(workspace) / "work/late.txt").exists())
                evidence = await control("execution_reconcile", id=fourth)
                self.assertTrue(evidence["safe_to_retry"], evidence)
                revision = (await control("context_get", id=context))["revision"]
                with self.assertRaises(PermissionError):
                    await control(
                        "context_continue",
                        context_id=context,
                        request_id=str(uuid.uuid4()),
                        revision=revision,
                        previous_execution_id=fourth,
                        text="Should not dispatch",
                    )
                self.assertEqual(len(gateway.requests), 7)
                old_context = context
                context, grant, retry = [str(uuid.uuid4()) for _ in range(3)]
                binding = {**binding, "context_id": context}
                await control(
                    "grant_issue",
                    grant_id=grant,
                    binding=binding,
                    actions=["execute", "read", "edit", "publish"],
                    expires_at=time.time() + 60,
                )
                await control(
                    "context_create",
                    context_id=context,
                    grant_id=grant,
                    expires_at=time.time() + 240,
                    model="model-a",
                    effort="low",
                )
                await control(
                    "context_run",
                    context_id=context,
                    request_id=retry,
                    revision=1,
                    text="Explicit fresh retry retaining existing workspace files",
                )
                result = await settled(retry)
                self.assertIn("FRESH_RETRY", result["result"])
                self.assertEqual((await usage.binding(retry)).work_id, a.work_id)
                self.assertEqual(service.records.get(fourth)["state"], "unknown")
                self.assertEqual(service.contexts.get("context", old_context)["data"]["state"], "uncertain")
                self.assertEqual(len(gateway.requests), 8)
                for item in (old_context, context):
                    revision = (await control("context_get", id=item))["revision"]
                    await control("context_close", id=item, revision=revision)
                revision = (await control("workspace_get", id=workspace))["revision"]
                released = await control("workspace_close", id=workspace, revision=revision)
                await control("workspace_collect", id=workspace, revision=released["revision"])
                self.assertFalse(service.broker.files.root(workspace).exists())
                archive_grant = str(uuid.uuid4())
                await control(
                    "grant_issue",
                    grant_id=archive_grant,
                    binding={**binding, "context_id": old_context},
                    actions=["read"],
                    expires_at=time.time() + 60,
                )
                content = await control("artifact_read", grant_id=archive_grant, id=artifact["id"])
                self.assertEqual(base64.b64decode(content["data"]), b"IMMUTABLE")
                self.assertTrue(content["archived"])

            finally:
                release_unknown.set()
                await runtime.close()
                store.db.close()
                await gateway.aclose()
