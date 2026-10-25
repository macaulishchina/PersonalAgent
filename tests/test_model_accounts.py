"""Actual Pi provider library, credential locks and agent processes; localhost only."""

from platform_support import private_mode

import asyncio
from dataclasses import replace
import json
import uuid
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from pi_gateway import FakeResponsesGateway, release_host_target, sse_text_reply, sse_tool_call
from assistant_runtime.core.models.settings import ModelSettings
from assistant_runtime.core.pi.accounts import NativeAccounts, installation
from assistant_runtime.core.pi.inference import complete, output_bound
from assistant_runtime.core.pi.rpc import PiClient
from assistant_runtime.core.ai_usage.service import UsageService


def configuration(auth="api_key"):
    return {
        "enabled": True,
        "accounts": {
            "personal": {
                "provider": "openai",
                "auth": auth,
                **({"key_env": "TEST_MODEL_KEY"} if auth == "api_key" else {"output_limit": "model"}),
            }
        },
        "providers": {"native": {"account": "personal", "enabled": True}},
        "entries": {
            "subscription": {
                "provider": "native",
                "model": "gpt-6.1-sol",
                "execution_validated": True,
                "structured": True,
                "images": True,
                "reasoning_efforts": ["low", "medium", "high"],
                "default_reasoning_effort": "medium",
            }
        },
    }


def local_package(root, installed, url):
    """Replace only provider endpoint/auth fixtures, retain the real SDK and file credential store."""
    package = root / "native-package/node_modules/@earendil-works/pi-coding-agent"
    # Match a lock-installed runtime with sibling/hoisted dependencies.
    api = package.parent / "pi-ai/dist"
    (api / "providers").mkdir(parents=True)
    actual = next(
        parent / "node_modules/@earendil-works/pi-ai/dist"
        for parent in (installed, *installed.parents)
        if (parent / "node_modules/@earendil-works/pi-ai/package.json").is_file()
    )
    (api.parent / "package.json").write_text(
        '{"name":"@earendil-works/pi-ai","type":"module","main":"./dist/index.js"}'
    )
    (api / "index.js").write_text(f"export * from {json.dumps((actual / 'index.js').as_uri())};")
    (api / "providers/all.js").write_text(f"""
import {{ openaiProvider }} from {json.dumps((actual / "providers/openai.js").as_uri())};
export function builtinProviders() {{
  const p = openaiProvider();
  return [{{ ...p, baseUrl: {json.dumps(url)}, getModels: () => p.getModels().map(m => ({{...m, baseUrl: {json.dumps(url)}}})),
    auth: {{...p.auth, oauth: {{...p.auth.oauth,
      refresh: async c => {{ await fetch({json.dumps(url + "/refresh")}, {{method:'POST', body:'{{}}'}}); return {{...c, access:'refreshed-synthetic', expires:Date.now()+3600000}}; }},
      toAuth: async c => ({{apiKey:c.access}})
    }} }}
  }}];
}}
""")
    (package / "dist/core").mkdir(parents=True)
    (package / "dist/core/auth-storage.js").write_text(
        f"export * from {json.dumps((installed / 'dist/core/auth-storage.js').as_uri())};"
    )
    (package / "package.json").write_text('{"type":"module","version":"1.4.0"}')
    return package


class AccountsTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.gateway = FakeResponsesGateway(
            lambda step, body: sse_text_reply(str(step), '{"answer":"synthetic"}')
        )
        package = local_package(self.root, await installation(), self.gateway.base_url)
        self.override = patch("assistant_runtime.core.pi.accounts.installation", return_value=package)
        self.override.start()
        (self.root / ".env").write_text("TEST_MODEL_KEY=sk-synthetic-test\n")
        self.settings = ModelSettings.parse(configuration())
        self.accounts = NativeAccounts(self.root, self.settings)

    async def asyncTearDown(self):
        await self.accounts.close()
        self.override.stop()
        await self.gateway.aclose()
        self.tmp.cleanup()

    def payload(self):
        return {
            "model": "subscription",
            "input": [
                {"role": "user", "content": [{"type": "input_text", "text": "Synthetic fixture only"}]}
            ],
            "reasoning": {"effort": "medium"},
            "max_output_tokens": 512,
        }

    async def test_auto_uses_native_defaults_without_forcing_an_effort(self):
        payload = self.payload()
        payload["reasoning"] = {"effort": "auto"}
        await complete(self.accounts, payload, "automatic-fixture")
        self.assertNotEqual(self.gateway.requests[0].get("reasoning", {}).get("effort"), "none")
        from assistant_runtime.core.pi.accounts import describe_model

        description = await describe_model("openai", "gpt-6.1-sol")
        self.assertEqual(description["context_window"], 272000)
        self.assertNotIn("none", description["reasoning_efforts"])
        self.assertEqual(len(self.gateway.requests), 1)  # description never authenticates or infers

    async def test_native_inference_schema_images_auth_and_catalog(self):
        payload = self.payload()
        payload["text"] = {
            "format": {
                "schema": {
                    "type": "object",
                    "properties": {"answer": {"type": "string"}},
                    "required": ["answer"],
                    "additionalProperties": False,
                }
            }
        }
        payload["input"][0]["content"].append(
            {
                "type": "input_image",
                "image_url": "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=",
            }
        )
        result = await complete(self.accounts, payload, "synthetic-session")
        self.assertEqual(result["text"], '{"answer":"synthetic"}')
        self.assertIsNotNone(result["usage"])
        self.assertEqual(await output_bound(self.accounts, payload), 512)
        request = self.gateway.requests[0]
        self.assertEqual(request["model"], "gpt-6.1-sol")
        self.assertEqual(request["text"]["format"]["type"], "json_schema")
        self.assertIn("input_image", json.dumps(request))
        self.assertEqual(
            {k.lower(): v for k, v in self.gateway.headers[0].items()}["authorization"],
            "Bearer sk-synthetic-test",
        )
        self.assertEqual(request["reasoning"]["effort"], "medium")
        self.assertEqual(request["max_output_tokens"], 512)
        worker = await self.accounts.account("personal")
        self.assertTrue((await worker.call("status"))["connected"])
        self.assertTrue(any(m["id"] == "gpt-6.1-sol" for m in await worker.call("catalog")))
        with self.assertRaisesRegex(RuntimeError, "validate_failed"):
            await self.accounts.route("subscription", "ultra")
        self.assertEqual(len(self.gateway.requests), 1)

    async def test_native_cache_affinity_and_cached_usage_survive_real_sdk(self):
        def reply(step, body):
            events = sse_text_reply(str(step), "OK")
            events[-1]["response"]["usage"] = {
                "input_tokens": 1000,
                "output_tokens": 2,
                "total_tokens": 1002,
                "input_tokens_details": {"cached_tokens": 900},
            }
            return events

        self.gateway.script_fn = reply
        payload = self.payload()
        for _ in range(2):
            result = await complete(self.accounts, payload, "stable-cache-session")
            self.assertEqual(result["usage"]["input_tokens"], 1000)
            self.assertEqual(result["usage"]["cached_input_tokens"], 900)
            timings = result["transport_timings"]
            self.assertGreater(timings["request_bytes"], 0)
            self.assertLessEqual(timings["prepared_ms"], timings["response_headers_ms"])
            self.assertLessEqual(timings["response_headers_ms"], timings["first_text_ms"])
            self.assertLessEqual(timings["first_text_ms"], timings["total_ms"])
        self.assertEqual(len(self.gateway.requests), 2)
        for request, headers in zip(self.gateway.requests, self.gateway.headers):
            self.assertEqual(request["prompt_cache_key"], "stable-cache-session")
            self.assertNotEqual(request.get("prompt_cache_options", {}).get("mode"), "explicit")
            self.assertEqual({k.lower(): v for k, v in headers.items()}["session_id"], "stable-cache-session")
        self.assertEqual(self.gateway.requests[0]["input"], self.gateway.requests[1]["input"])
        self.assertEqual(self.gateway.step, 2)  # No automatic warming or replay requests.

    async def test_missing_oauth_never_uses_environment_key_or_global_codex(self):
        oauth = NativeAccounts(self.root, ModelSettings.parse(configuration("oauth")))
        try:
            worker = await oauth.account("personal")
            self.assertFalse((await worker.call("status"))["connected"])
            with self.assertRaises(RuntimeError):
                await complete(oauth, self.payload(), "synthetic")
            self.assertEqual(self.gateway.requests, [])
            metadata = json.dumps(await worker.call("status"))
            self.assertNotIn("sk-", metadata)
        finally:
            await oauth.close()

    async def test_cross_process_refresh_is_serialized_and_persisted_privately(self):
        settings = ModelSettings.parse(configuration("oauth"))
        path = self.root / "state/model-accounts/personal"
        path.mkdir(parents=True, mode=0o700)
        (path / "auth.json").write_text(
            json.dumps(
                {
                    "openai": {
                        "type": "oauth",
                        "refresh": "synthetic-refresh",
                        "access": "expired",
                        "expires": 0,
                    }
                }
            )
        )
        (path / "auth.json").chmod(0o600)
        second = NativeAccounts(self.root, settings)
        first = NativeAccounts(self.root, settings)
        try:
            results = await asyncio.gather(
                complete(first, self.payload(), "one"), complete(second, self.payload(), "two")
            )
            self.assertEqual(len(results), 2)
            # The fixture's refresh request has no model; two model calls share one locked refresh.
            self.assertEqual(sum("model" not in p for p in self.gateway.requests), 1)
            self.assertEqual(sum("model" in p for p in self.gateway.requests), 2)
            self.assertEqual(private_mode(path / "auth.json"), 0o600)
            self.assertEqual(private_mode(path), 0o700)
        finally:
            await first.close()
            await second.close()

    async def test_real_pi_tools_restart_and_shared_usage_limit(self):
        target = self.root / "actual.txt"
        self.gateway.script_fn = (
            lambda step, body: sse_tool_call("write1", "write", {"path": str(target), "content": "verified"})
            if step == 1
            else sse_text_reply(str(step), "Completed")
        )
        usage = UsageService(self.root / "usage")
        await usage.start()
        client = PiClient(
            cwd=str(self.root),
            settings=self.settings,
            environment={"ASSISTANT_MODEL_GATEWAY_KEY": "synthetic"},
            runtime_dir=self.root / "executions",
            usage=usage,
            accounts=self.accounts,
        )
        try:
            await client.start()
            ident = (
                await client.request(
                    "session.create",
                    {"model": "subscription", "config": {"model_reasoning_effort": "medium"}},
                )
            )["session"]["id"]
            await client.request(
                "run.start",
                {"session_id": ident, "input": [{"type": "text", "text": "Synthetic file operation"}]},
            )
            async with asyncio.timeout(20):
                while True:
                    event = await client.events.get()
                    if event["kind"] == "run.completed":
                        run = event["data"]["run"]
                        break
            self.assertEqual(run["status"], "completed", run)
            self.assertEqual(target.read_text(), "verified")
            self.assertEqual(len(self.gateway.requests), 2)
            self.assertEqual(run["ai_usage"]["requests"], 2)
            await client.close()
            await client.start()
            await client.request("session.resume", {"session_id": ident})
            usage.settings = replace(usage.settings, max_requests=1)
            usage.ledger.settings = usage.settings
            self.gateway.script_fn = lambda step, body: sse_tool_call(
                str(step), "read", {"path": str(target)}
            )
            await client.request(
                "run.start",
                {"session_id": ident, "input": [{"type": "text", "text": "Synthetic looping tool request"}]},
            )
            async with asyncio.timeout(20):
                while True:
                    event = await client.events.get()
                    if event["kind"] == "run.completed":
                        stopped = event["data"]["run"]
                        break
            self.assertNotEqual(stopped["status"], "completed")
            self.assertEqual(len(self.gateway.requests), 3)
        finally:
            await client.close()
            await usage.close()

    async def test_native_stream_failure_does_not_lock_new_foreground_requests(self):
        self.gateway.script_fn = lambda step, body: (
            sse_text_reply("truncated", "partial")[:-1]
            if step == 1
            else sse_text_reply("new-request", "new answer")
        )
        usage = UsageService(self.root / "stream-usage")
        await usage.start()
        client = PiClient(
            cwd=str(self.root),
            settings=self.settings,
            environment={"ASSISTANT_MODEL_GATEWAY_KEY": "synthetic"},
            runtime_dir=self.root / "executions",
            usage=usage,
            accounts=self.accounts,
        )
        try:
            await client.start()
            ident = (
                await client.request(
                    "session.create",
                    {"model": "subscription", "config": {"model_reasoning_effort": "medium"}},
                )
            )["session"]["id"]
            runs = []
            for prompt in ("synthetic failed request", "new explicit question"):
                await client.request(
                    "run.start", {"session_id": ident, "input": [{"type": "text", "text": prompt}]}
                )
                async with asyncio.timeout(20):
                    while True:
                        event = await client.events.get()
                        if event["kind"] == "run.completed":
                            runs.append(event["data"]["run"])
                            break
            self.assertEqual([run["status"] for run in runs], ["failed", "completed"])
            self.assertEqual(runs[0]["error"]["code"], "generation_failed")
            self.assertEqual(len(self.gateway.requests), 2)
            self.assertEqual((await usage.snapshot(runs[0]["id"]))["unknown_execution_requests"], 1)
        finally:
            await client.close()
            await usage.close()

    async def test_native_compaction_timeout_keeps_context_and_can_resume(self):
        from assistant_runtime.core.pi.rpc import RpcError

        settings = replace(self.settings, timeout_seconds=1, compaction_timeout_seconds=2)
        usage = UsageService(self.root / "compaction-usage")
        await usage.start()
        client = PiClient(
            cwd=str(self.root),
            settings=settings,
            environment={"ASSISTANT_MODEL_GATEWAY_KEY": "synthetic"},
            runtime_dir=self.root / "executions",
            usage=usage,
            accounts=self.accounts,
        )
        try:
            await client.start()
            ident = (
                await client.request(
                    "session.create",
                    {
                        "model": "subscription",
                        "budget_id": "native-compaction",
                        "config": {"model_reasoning_effort": "medium"},
                    },
                )
            )["session"]["id"]
            worker = client.workers[ident]
            path = Path(worker.meta["agent_dir"]) / "settings.json"
            config = json.loads(path.read_text())
            config["compaction"]["keepRecentTokens"] = 100
            path.write_text(json.dumps(config))

            async def prompt():
                await client.request(
                    "run.start",
                    {
                        "session_id": ident,
                        "input": [{"type": "text", "text": "synthetic original context " * 100}],
                    },
                )
                async with asyncio.timeout(15):
                    while True:
                        event = await client.events.get()
                        if event["kind"] == "run.completed":
                            self.assertEqual(event["data"]["run"]["status"], "completed", event)
                            return

            await prompt()
            await prompt()

            def too_slow(step, body):
                import time

                time.sleep(2.3)
                return []

            self.gateway.script_fn = too_slow
            with self.assertRaises(RpcError):
                await worker.command({"type": "compact"})
            snapshot = await usage.snapshot("native-compaction")
            self.assertEqual(snapshot["unknown_requests"], 1)
            self.assertEqual(snapshot["unknown_execution_requests"], 0)

            def slow_success(step, body):
                import time

                time.sleep(1.3)
                return sse_text_reply("summary", "Original task context preserved")

            self.gateway.script_fn = slow_success
            await worker.command({"type": "compact"})
            self.assertEqual((await usage.snapshot("native-compaction"))["requests"], 4)
            self.gateway.script_fn = lambda step, body: sse_text_reply("resumed", "done")
            await prompt()
        finally:
            await client.close()
            await usage.close()

    async def test_explicit_rejection_is_sanitized_without_retry(self):
        from assistant_runtime.api.models import ModelCallError

        self.gateway.script_fn = lambda step, body: (402, {"error": {"message": "private test detail"}})
        with self.assertRaises(ModelCallError) as caught:
            await complete(self.accounts, self.payload(), "synthetic")
        self.assertEqual(str(caught.exception), "model_http_402")
        self.assertTrue(caught.exception.fallback_allowed)
        self.assertNotIn("private", str(caught.exception))
        self.assertEqual(len(self.gateway.requests), 1)

    async def test_cancel_aborts_native_request_and_retains_unknown_receipt(self):
        gate = threading.Event()
        self.gateway.script_fn = lambda step, body: (gate.wait(10), sse_text_reply("late", "Late"))[1]
        usage = UsageService(self.root / "usage")
        await usage.start()
        client = PiClient(
            cwd=str(self.root),
            settings=self.settings,
            environment={"ASSISTANT_MODEL_GATEWAY_KEY": "synthetic"},
            runtime_dir=self.root / "executions",
            usage=usage,
            accounts=self.accounts,
        )
        try:
            await client.start()
            ident = (
                await client.request(
                    "session.create",
                    {
                        "model": "subscription",
                        "budget_id": "cancelled-work",
                        "config": {"model_reasoning_effort": "medium"},
                    },
                )
            )["session"]["id"]
            run = (
                await client.request(
                    "run.start",
                    {"session_id": ident, "input": [{"type": "text", "text": "Synthetic cancellation"}]},
                )
            )["run"]["id"]
            async with asyncio.timeout(10):
                while not self.gateway.requests:
                    await asyncio.sleep(0.01)
            await client.request("run.cancel", {"session_id": ident, "run_id": run})
            async with asyncio.timeout(3):
                while True:
                    snapshot = await usage.snapshot("cancelled-work")
                    if not snapshot["inflight"]:
                        break
                    await asyncio.sleep(0.01)
            self.assertEqual(snapshot["unknown_execution_requests"], 1)
            self.assertEqual(len(self.gateway.requests), 1)
            worker = await self.accounts.account("personal")
            self.assertTrue((await worker.call("status"))["connected"])
        finally:
            gate.set()
            await client.close()
            await usage.close()

    async def test_native_no_tool_inference_rejects_tool_output(self):
        target = self.root / "must-not-exist.txt"
        self.gateway.script_fn = lambda step, body: sse_tool_call(
            "not-authorized", "write", {"path": str(target), "content": "not-authorized"}
        )
        with self.assertRaises(RuntimeError):
            await complete(self.accounts, self.payload(), "synthetic")
        self.assertFalse(target.exists())
        self.assertEqual(len(self.gateway.requests), 1)

    async def test_native_main_delegates_gateway_task_with_shared_work_identity(self):
        from assistant_runtime.app.config import Config
        from scripts.verify_pi_host import verify
        from test_pi_skeleton import configuration as gateway_config

        counts = {}

        def script(step, body):
            model = body["model"]
            counts[model] = counts.get(model, 0) + 1
            target = release_host_target(body)
            if model == "gpt-6.1-sol" and counts[model] == 1:
                return sse_tool_call(
                    "discover", "tool_search", {"query": "create task request_id title prompt", "limit": 8}
                )
            if model == "gpt-6.1-sol" and counts[model] == 2:
                return sse_tool_call(
                    "delegate",
                    "mcp__tasks__create_task",
                    {
                        "request_id": str(uuid.uuid4()),
                        "feedback_owner": "conversation",
                        "title": "Synthetic mixed providers",
                        "prompt": "Write SYNTHETIC_OK to " + target,
                        "execution": {
                            "model": "model-b",
                            "reasoning_effort": "low",
                            "model_source": "user",
                            "effort_source": "user",
                            "reason": "Synthetic file readback",
                        },
                    },
                )
            if model == "model-b" and counts[model] == 1:
                return sse_tool_call("write", "write", {"path": target, "content": "SYNTHETIC_OK"})
            return sse_text_reply("done" + str(step), "已完成")

        self.gateway.script_fn = script
        raw = configuration()
        gateway = gateway_config(self.gateway.base_url)
        raw["gateway_url"] = self.gateway.base_url
        raw["providers"].update(gateway["providers"])
        raw["entries"].update(gateway["entries"])
        (self.root / ".env").write_text(
            "TEST_MODEL_KEY=sk-synthetic-test\nASSISTANT_MODEL_GATEWAY_KEY=synthetic-key\n"
        )
        config = Config(root=self.root, model_api=raw)

        result = await verify(config, "subscription", "model-b", effort="low", max_requests=6)
        self.assertEqual(result["state"], "completed", result)
        self.assertEqual(result["requests"], 5)
        self.assertEqual(result["callers"], 2)
        self.assertTrue(result["shared_work_id"])

    async def test_model_service_native_memory_receipt_is_idempotent_and_metered(self):
        from types import SimpleNamespace
        from usage_fixture import attach_usage
        from assistant_runtime.api.models import ModelRequest
        from assistant_runtime.kernel.runtime import Runtime
        from assistant_runtime.core.models.plugin import ModelsPlugin
        from assistant_runtime.app.store import Store

        store = Store(self.root / "models.sqlite3")
        runtime = Runtime(store)
        raw = configuration()
        raw["memory"] = "subscription"
        host = SimpleNamespace(
            config=SimpleNamespace(root=self.root, state_dir=self.root / "state", model_api=raw),
            store=store,
            runtime=runtime,
        )
        (self.root / ".env").write_text(
            "TEST_MODEL_KEY=sk-synthetic-test\nASSISTANT_MODEL_GATEWAY_KEY=synthetic-key\n"
        )
        attach_usage(host, runtime)
        runtime.add(ModelsPlugin(host))
        await runtime.start()
        request = ModelRequest(
            "infer",
            {
                "request_id": str(uuid.uuid4()),
                "model": "subscription",
                "text": "Synthetic structured extraction",
                "schema": {
                    "type": "object",
                    "properties": {"answer": {"type": "string"}},
                    "required": ["answer"],
                    "additionalProperties": False,
                },
            },
        )
        try:
            result = await runtime.invoke(request)
            repeated = await runtime.invoke(request)
            self.assertEqual(result["state"], "completed")
            self.assertEqual(result["data"], {"answer": "synthetic"})
            self.assertEqual(result["usage_call_id"], repeated["usage_call_id"])
            self.assertEqual(len(self.gateway.requests), 1)
            calls = await host.ai_usage.service.call("query", {})
            self.assertEqual(len(calls["items"]), 1)
            self.assertEqual(calls["items"][0]["state"], "completed")
        finally:
            await runtime.close()
            store.db.close()


class SettingsTest(unittest.TestCase):
    def test_subscription_requires_explicit_provider_output_limit(self):
        value = configuration("oauth")
        del value["accounts"]["personal"]["output_limit"]
        with self.assertRaisesRegex(ValueError, "request caps unsupported"):
            ModelSettings.parse(value)

    def test_native_credentials_never_enter_litellm_configuration(self):
        from assistant_runtime.core.models.gateway import configuration as gateway

        settings = ModelSettings.parse(configuration())
        self.assertEqual(gateway(settings)["model_list"], [])


if __name__ == "__main__":
    unittest.main()
