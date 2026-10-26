"""Real pinned Pi protocol adapters against isolated HTTP endpoints; no paid requests."""

import asyncio
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import sqlite3
from copy import deepcopy
from types import SimpleNamespace

from aiohttp import web
from assistant_runtime.core.models.settings import ModelSettings
from assistant_runtime.core.models.client import ModelClient
from assistant_runtime.core.pi.accounts import NativeAccounts
from assistant_runtime.api.models import ModelCallError
from assistant_runtime.api.configuration import ConfigurationPending
from assistant_runtime.core.models.plugin import ModelsPlugin
from pi_gateway import sse_text_reply


class ConnectionsTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.area = tempfile.TemporaryDirectory()
        self.root = Path(self.area.name)
        (self.root / ".env").write_text("TEST_KEY=private-synthetic-key\n")
        self.requests = []
        self.mode = "ok"
        self.protocol = "openai_responses"
        self.gate = asyncio.Event()
        app = web.Application()
        app.router.add_route("*", "/{path:.*}", self.handle)
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/v1"
        self.clients = []

    async def asyncTearDown(self):
        self.gate.set()
        for client in self.clients:
            await client.close()
        await self.runner.cleanup()
        self.area.cleanup()

    async def client(
        self, protocol="openai_responses", *, model="synthetic", efforts=None, **provider_options
    ):
        self.protocol = protocol
        settings = ModelSettings.parse(
            {
                "enabled": True,
                "providers": {
                    "p": {
                        "protocol": protocol,
                        "base_url": self.url,
                        "key_env": "TEST_KEY",
                        "enabled": True,
                        **provider_options,
                    }
                },
                "entries": {
                    "m": {
                        "provider": "p",
                        "model": model,
                        "images": True,
                        "structured": True,
                        "reasoning_efforts": efforts
                        if efforts is not None
                        else ["none", "low", "high", "max"],
                        "default_reasoning_effort": "auto",
                    }
                },
            }
        )
        client = ModelClient(settings, accounts=NativeAccounts(self.root, settings))
        self.clients.append(client)
        await client.start()
        return client

    def payload(self, effort="auto"):
        return {
            "model": "m",
            "reasoning": {"effort": effort},
            "max_output_tokens": 128,
            "input": [{"role": "user", "content": [{"type": "input_text", "text": "Synthetic test"}]}],
        }

    async def handle(self, request):
        self.requests.append((request.path, dict(request.headers), await request.json()))
        if self.mode == "reject":
            return web.Response(status=429, text="private-synthetic-key must never reach diagnostics")
        if self.mode == "slow":
            await self.gate.wait()
        if self.mode == "redirect":
            return web.Response(status=307, headers={"Location": self.url + "/unexpected"})
        if self.protocol == "openai_responses":
            events = sse_text_reply("synthetic-response", "OK")
        elif self.protocol == "openai_chat":
            events = [
                {
                    "id": "synthetic",
                    "choices": [
                        {"index": 0, "delta": {"role": "assistant", "content": "OK"}, "finish_reason": None}
                    ],
                },
                {
                    "id": "synthetic",
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
                },
            ]
        elif self.protocol == "anthropic":
            events = [
                {
                    "type": "message_start",
                    "message": {
                        "id": "synthetic",
                        "role": "assistant",
                        "content": [],
                        "model": "synthetic",
                        "usage": {"input_tokens": 3, "output_tokens": 0},
                    },
                },
                {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
                {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "OK"}},
                {"type": "content_block_stop", "index": 0},
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "end_turn"},
                    "usage": {"output_tokens": 1},
                },
                {"type": "message_stop"},
            ]
        else:
            events = [
                {
                    "candidates": [
                        {"content": {"role": "model", "parts": [{"text": "OK"}]}, "finishReason": "STOP"}
                    ],
                    "usageMetadata": {"promptTokenCount": 3, "candidatesTokenCount": 1, "totalTokenCount": 4},
                }
            ]
        body = "".join(
            ("event: " + e["type"] + "\n" if "type" in e else "") + "data: " + json.dumps(e) + "\n\n"
            for e in events
        )
        return web.Response(text=body, content_type="text/event-stream")

    async def test_all_four_protocols_stream_without_external_service(self):
        for protocol in ("openai_responses", "openai_chat", "anthropic", "gemini"):
            with self.subTest(protocol=protocol):
                client = await self.client(protocol)
                self.assertEqual((await client.complete(self.payload(), session_id="one"))["text"], "OK")
        self.assertEqual(len(self.requests), 4)

    async def test_effort_selection_auto_and_explicit_levels(self):
        client = await self.client()
        for level in ("auto", "none", "low", "high", "max"):
            await client.complete(self.payload(level), session_id="one")
            wire = self.requests[-1][2]
            if level == "auto":
                self.assertNotEqual(wire.get("reasoning", {}).get("effort"), "none")
            else:
                self.assertEqual(wire["reasoning"]["effort"], level)
        with self.assertRaises(RuntimeError):
            await client.complete(self.payload("medium"), session_id="one")
        self.assertEqual(len(self.requests), 5)

    async def test_private_auth_and_stable_session_header(self):
        client = await self.client(auth_scheme="bearer", session_header="x-session-id")
        for session in ("one", "one", "two"):
            await client.complete(self.payload(), session_id=session)
        headers = [r[1] for r in self.requests]
        self.assertEqual(headers[0]["Authorization"], "Bearer private-synthetic-key")
        self.assertEqual(headers[0]["x-session-id"], hashlib.sha256(b"one").hexdigest())
        self.assertEqual(headers[0]["x-session-id"], headers[1]["x-session-id"])
        self.assertNotEqual(headers[0]["x-session-id"], headers[2]["x-session-id"])
        self.assertNotIn("one", json.dumps(self.requests[0][2]["input"]))

    async def test_gemini_declared_efforts_reach_wire_and_auto_remains_default(self):
        from assistant_runtime.core.models.capabilities import profile
        from unittest.mock import patch

        catalog = {
            "revision": "fixture",
            "models": {"google/example": {"reasoning": True, "reasoning_efforts": ["low", "medium", "high"]}},
        }
        for model in ("gemini-4-argon", "gemini-2.5-flash"):
            with (
                self.subTest(model=model),
                patch("assistant_runtime.core.models.capabilities.public_catalog", return_value=catalog),
            ):
                metadata = profile(
                    {"protocol": "gemini", "base_url": self.url}, model, reference="google/example"
                )
                client = await self.client("gemini", model=model, efforts=metadata["reasoning_efforts"])
                for level in ("auto", "low", "medium", "high"):
                    await client.complete(self.payload(level), session_id="one")
                    config = self.requests[-1][2]["generationConfig"]
                    if level == "auto":
                        self.assertNotIn("thinkingConfig", config)
                    elif model == "gemini-4-argon":
                        self.assertEqual(config["thinkingConfig"]["thinkingLevel"], level.upper())
                        self.assertNotIn("thinkingBudget", config["thinkingConfig"])
                    else:
                        self.assertGreater(config["thinkingConfig"]["thinkingBudget"], 0)
                        self.assertNotIn("thinkingLevel", config["thinkingConfig"])
                count = len(self.requests)
                with self.assertRaises(RuntimeError):
                    await client.complete(self.payload("max"), session_id="one")
                self.assertEqual(len(self.requests), count)

    async def test_structured_output_preserves_schema_all_protocols(self):
        schema = {
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
            "additionalProperties": False,
        }
        for protocol in ("openai_responses", "openai_chat", "anthropic", "gemini"):
            with self.subTest(protocol=protocol):
                client = await self.client(protocol)
                payload = self.payload()
                payload["text"] = {"format": {"schema": schema}}
                await client.complete(payload, session_id="one")
                self.assertIn('"required": ["value"]', json.dumps(self.requests[-1][2]))

    async def test_rejection_never_retries_or_exposes_upstream_body(self):
        client = await self.client()
        self.mode = "reject"
        with self.assertRaises(ModelCallError) as caught:
            await client.complete(self.payload(), session_id="one")
        self.assertNotIn("private-synthetic-key", str(caught.exception))
        self.assertEqual(len(self.requests), 1)

    async def test_cancel_does_not_replay_and_next_request_succeeds(self):
        client = await self.client()
        self.mode = "slow"
        job = asyncio.create_task(client.complete(self.payload(), session_id="one"))
        async with asyncio.timeout(8):
            while not self.requests:
                await asyncio.sleep(0.02)
        job.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await job
        self.mode = "ok"
        self.gate.set()
        self.assertEqual((await client.complete(self.payload(), session_id="two"))["text"], "OK")
        self.assertEqual(len(self.requests), 2)

    async def test_refresh_credentials_changes_new_client_only(self):
        old = await self.client()
        (self.root / ".env").write_text("TEST_KEY=changed-synthetic-key\n")
        new = await self.client()
        await old.complete(self.payload(), session_id="old")
        await new.complete(self.payload(), session_id="new")
        self.assertEqual(self.requests[0][1]["Authorization"], "Bearer private-synthetic-key")
        self.assertEqual(self.requests[1][1]["Authorization"], "Bearer changed-synthetic-key")

    async def test_model_update_prepares_commits_rolls_back_and_waits_for_active_calls(self):
        old = await self.client()
        raw = {
            "enabled": True,
            "providers": deepcopy(old.settings.providers),
            "entries": deepcopy(old.settings.entries),
        }
        db = sqlite3.connect(":memory:")
        host = SimpleNamespace(
            config=SimpleNamespace(root=self.root, model_api=raw), store=SimpleNamespace(db=db)
        )
        plugin = ModelsPlugin(host, old)
        candidate = SimpleNamespace(root=self.root, model_api=deepcopy(raw), pi_binary="pi")
        try:
            candidate.model_api["providers"]["p"]["key_env"] = "MISSING_SYNTHETIC_KEY"
            with self.assertRaises(ValueError):
                await plugin.prepare_configuration(candidate)
            self.assertIs(plugin.client, old)
            self.assertEqual((await old.complete(self.payload()))["text"], "OK")
            candidate.model_api = deepcopy(raw)
            (self.root / ".env").write_text("TEST_KEY=changed-synthetic-key\n")
            plugin.pending = 1
            with self.assertRaises(ConfigurationPending):
                await plugin.prepare_configuration(candidate)
            plugin.pending = 0
            update = await plugin.prepare_configuration(candidate)
            await update.commit()
            await update.rollback()
            await update.release(False)
            self.assertIs(plugin.client, old)
            await old.complete(self.payload())
            self.assertEqual(self.requests[-1][1]["Authorization"], "Bearer private-synthetic-key")
            update = await plugin.prepare_configuration(candidate)
            await update.commit()
            await update.release(True)
            self.clients.append(plugin.client)
            await plugin.client.complete(self.payload())
            self.assertEqual(self.requests[-1][1]["Authorization"], "Bearer changed-synthetic-key")
            self.assertFalse(old.accounts.workers)
        finally:
            db.close()

    async def test_aliases_of_same_supplier_model_keep_their_own_capabilities(self):
        client = await self.client()
        settings = client.settings
        entries = deepcopy(settings.entries)
        entries["second"] = {**entries["m"], "reasoning_efforts": ["low"], "default_reasoning_effort": "low"}
        from dataclasses import replace

        settings = replace(settings, entries=entries)
        other = ModelClient(settings, accounts=NativeAccounts(self.root, settings))
        self.clients.append(other)
        await other.start()
        await other.complete(self.payload("high"))
        payload = self.payload("low")
        payload["model"] = "second"
        await other.complete(payload)
        self.assertEqual([r[2]["reasoning"]["effort"] for r in self.requests], ["high", "low"])
        self.assertEqual([r[2]["model"] for r in self.requests], ["synthetic", "synthetic"])
        payload["reasoning"]["effort"] = "high"
        with self.assertRaises(RuntimeError):
            await other.complete(payload)
        self.assertEqual(len(self.requests), 2)

    async def test_api_connection_does_not_collide_with_account_name(self):
        from dataclasses import replace

        client = await self.client()
        settings = replace(
            client.settings,
            accounts={"api-p": {"provider": "openai", "auth": "api_key", "key_env": "TEST_KEY"}},
        )
        accounts = NativeAccounts(self.root, settings)
        try:
            direct = await accounts.connection("p")
            native = await accounts.account("api-p")
            self.assertIsNot(direct, native)
            self.assertNotEqual(direct.folder, native.folder)
            self.assertEqual(direct.spec["protocol"], "openai_responses")
            self.assertEqual(native.spec["provider"], "openai")
            self.assertEqual(self.requests, [])
        finally:
            await accounts.close()
