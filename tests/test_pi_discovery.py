"""Native Pi search/loading, bounded declarations and unchanged authorization."""

import json
import asyncio
from dataclasses import replace
import sys
import unittest

from pi_gateway import sse_text_reply, sse_tool_call, sse_tool_batch
import test_pi_skeleton as fixture


class DiscoveryTest(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.PiSkeletonTest.asyncSetUp
    asyncTearDown = fixture.PiSkeletonTest.asyncTearDown
    session = fixture.PiSkeletonTest.session
    completed = fixture.PiSkeletonTest.completed

    async def configured(
        self, *, tools=None, limit=2, second=False, stable=False, approval="approve", read_only=False
    ):
        self.client.settings = replace(self.settings, max_loaded_tools=limit)
        script = self.root / "catalog.py"
        script.write_text(
            "from fastmcp import FastMCP\nfrom pathlib import Path\nm=FastMCP('catalog')\n"
            "for i in range(30):\n"
            " def make(i):\n"
            "  def action():\n"
            f"   with Path({str(self.root / 'calls.txt')!r}).open('a') as f: f.write(str(i)+'\\n')\n"
            "   if i == 28: raise RuntimeError('Tool mcp__catalog__action_28 not found')\n"
            "   return 'verified:'+str(i)\n"
            "  return action\n"
            f" m.tool(name='action_'+str(i),annotations={{'readOnlyHint':{read_only!r},'destructiveHint':False}},description='Catalog action '+str(i)+'\\nSCHEMA_SENTINEL '+('catalog operation '+str(i)+' ')*80)(make(i))\n"
            "m.run(transport='stdio',show_banner=False)\n"
        )
        config = {
            "model_reasoning_effort": "low",
            "mcp_servers": {
                "catalog": {
                    "command": sys.executable,
                    "args": [str(script)],
                    "enabled": True,
                    "disabled_tools": ["action_29"],
                    "default_tools_approval_mode": approval,
                },
            },
        }
        if tools is not None:
            config["allowed_tools"] = tools
        if second:
            config["mcp_servers"]["alternate-catalog"] = dict(config["mcp_servers"]["catalog"])
        return await self.session(config=config, _stable_execution_prompt=stable)

    async def run_prompt(self, ident):
        await self.client.request(
            "run.start", {"session_id": ident, "input": [{"type": "text", "text": "Synthetic catalog test"}]}
        )
        result = await self.completed()
        self.assertEqual(result["status"], "completed", result)

    async def test_invalid_policy_cannot_silently_drop_extension_guards(self):
        from assistant_runtime.core.pi.rpc import RpcError

        ident = await self.configured(approval="invalid")
        with self.assertRaisesRegex(RpcError, "extension failed"):
            await self.run_prompt(ident)
        self.assertEqual(self.gateway.requests, [])
        self.assertFalse((self.root / "calls.txt").exists())

    async def test_rejected_service_cannot_discover_or_execute_tools(self):
        ident = await self.configured(approval="reject")

        def script(step, body):
            if step == 1:
                return sse_tool_call("discover", "tool_search", {"query": "action_3", "limit": 1})
            if step == 2:
                return sse_tool_call("blocked", "mcp__catalog__action_3", {})
            self.assertTrue("disabled by policy" in str(body) or "not found" in str(body), body)
            return sse_text_reply("done", "Tool is not allowed")

        self.gateway.script_fn = script
        await self.run_prompt(ident)
        self.assertEqual(len(self.gateway.requests), 3)
        self.assertFalse((self.root / "calls.txt").exists())

    async def test_native_search_scope_and_real_tool_call(self):
        ident = await self.configured(tools=["mcp__catalog__action_3"])

        def script(step, body):
            names = [t.get("name", "") for t in body.get("tools", [])]
            self.assertNotIn("mcp__catalog__action_29", names)
            self.assertNotIn("mcp__catalog__action_4", names)
            if step == 1:
                self.assertIn("tool_search", names)
                self.assertNotIn("mcp__catalog__action_3", names)
                return sse_tool_call("search", "tool_search", {"query": "action_3", "limit": 1})
            if step == 2:
                self.assertIn("mcp__catalog__action_3", names)
                return sse_tool_call("call", "mcp__catalog__action_3", {})
            return sse_text_reply("done", "verified")

        self.gateway.script_fn = script
        await self.run_prompt(ident)
        self.assertIn("verified:3", json.dumps(self.gateway.requests[-1]))
        self.assertEqual(len(self.gateway.requests), 3)
        await self.client.close()
        await self.client.start()
        await self.client.request("session.resume", {"session_id": ident})
        self.gateway.script_fn = lambda step, body: sse_text_reply("resumed", "done")
        await self.run_prompt(ident)
        self.assertFalse(
            any(t.get("name", "").startswith("mcp__") for t in self.gateway.requests[-1].get("tools", []))
        )

    async def test_search_cannot_expand_scope_or_load_an_unbounded_batch(self):
        ident = await self.configured(tools=["mcp__catalog__action_3"])

        def script(step, body):
            names = [t.get("name", "") for t in body.get("tools", [])]
            self.assertNotIn("mcp__catalog__action_29", names)
            self.assertNotIn("mcp__catalog__action_4", names)
            if step == 1:
                return sse_tool_call("oversized", "tool_search", {"query": "action", "limit": 1000})
            if step == 2:
                self.assertFalse(any(n.startswith("mcp__") for n in names))
                self.assertIn("limit: must be <= 2", str(body))
                return sse_tool_call("forged", "mcp__catalog__action_4", {})
            if step == 3:
                self.assertIn("not found", str(body))
                return sse_tool_call("allowed", "tool_search", {"query": "action_3", "limit": 1})
            return sse_text_reply("done", "done")

        self.gateway.script_fn = script
        await self.run_prompt(ident)
        self.assertEqual(len(self.gateway.requests), 4)

    async def test_exact_name_beats_noisy_search_and_already_loaded_match_is_retained(self):
        ident = await self.configured()

        def script(step, body):
            names = [t.get("name", "") for t in body.get("tools", [])]
            if step == 1:
                return sse_tool_call(
                    "search", "tool_search", {"query": "catalog action_3 background execution", "limit": 1}
                )
            if step == 2:
                self.assertIn("mcp__catalog__action_3", names)
                return sse_tool_call("repeat", "tool_search", {"query": "mcp__catalog__action_3", "limit": 1})
            if step == 3:
                self.assertIn("mcp__catalog__action_3", names)
                self.assertEqual(sum(n.startswith("mcp__") for n in names), 1)
                return sse_tool_call("call", "mcp__catalog__action_3", {})
            return sse_text_reply("done", "done")

        self.gateway.script_fn = script
        await self.run_prompt(ident)
        self.assertIn("verified:3", json.dumps(self.gateway.requests[-1]))
        self.assertEqual(len(self.gateway.requests), 4)

    async def test_unshown_call_executes_once_without_extra_model_round(self):
        ident = await self.configured()

        def script(step, body):
            names = [t.get("name", "") for t in body.get("tools", [])]
            if step == 1:
                return sse_tool_call("warm", "tool_search", {"query": "action_1", "limit": 1})
            if step == 2:
                self.assertNotIn("mcp__catalog__action_3", names)
                return sse_tool_call("unloaded", "mcp__catalog__action_3", {})
            if step == 3:
                self.assertIn("mcp__catalog__action_3", names)
                self.assertNotIn("this call did not execute", str(body))
                self.assertIn("verified:3", str(body))
            return sse_text_reply("done", "done")

        self.gateway.script_fn = script
        await self.run_prompt(ident)
        self.assertEqual(len(self.gateway.requests), 3)
        self.assertEqual(json.dumps(self.gateway.requests[-1]).count("verified:3"), 1)
        self.assertEqual((self.root / "calls.txt").read_text().splitlines(), ["3"])

    async def test_ambiguous_short_name_requires_namespace_and_metadata_search_still_works(self):
        ident = await self.configured(second=True)

        def script(step, body):
            names = [t.get("name", "") for t in body.get("tools", [])]
            if step == 1:
                return sse_tool_call("ambiguous", "tool_search", {"query": "action_3", "limit": 2})
            if step == 2:
                self.assertFalse(any(n.startswith("mcp__") for n in names))
                self.assertIn("Tool name is ambiguous", str(body))
                return sse_tool_call(
                    "qualified", "tool_search", {"query": "alternate-catalog.action_3", "limit": 1}
                )
            if step == 3:
                self.assertIn("mcp__alternate_catalog__action_3", names)
                self.assertNotIn("mcp__catalog__action_3", names)
                return sse_tool_call("call", "mcp__alternate_catalog__action_3", {})
            return sse_text_reply("done", "done")

        self.gateway.script_fn = script
        await self.run_prompt(ident)
        self.assertEqual((self.root / "calls.txt").read_text().splitlines(), ["3"])

    async def test_native_metadata_search_and_real_business_failure_are_not_replayed(self):
        ident = await self.configured()

        def script(step, body):
            names = [t.get("name", "") for t in body.get("tools", [])]
            if step == 1:
                return sse_tool_call("semantic", "tool_search", {"query": "Catalog operation 28", "limit": 1})
            if step == 2:
                self.assertIn("mcp__catalog__action_28", names)
                return sse_tool_call("fails", "mcp__catalog__action_28", {})
            self.assertNotIn("this call did not execute", str(body))
            return sse_text_reply("done", "failure reported")

        self.gateway.script_fn = script
        await self.run_prompt(ident)
        self.assertEqual(len(self.gateway.requests), 3)
        self.assertEqual((self.root / "calls.txt").read_text().splitlines(), ["28"])

    async def test_unknown_outcome_is_not_dispatched_again_in_the_turn(self):
        ident = await self.configured()

        def script(step, body):
            if step == 1:
                return sse_tool_call("load", "tool_search", {"query": "action_28", "limit": 1})
            if step in (2, 3):
                return sse_tool_call(str(step), "mcp__catalog__action_28", {})
            self.assertIn("prior_result_unknown", str(body))
            return sse_text_reply("done", "Please inspect the operation")

        self.gateway.script_fn = script
        await self.run_prompt(ident)
        self.assertEqual((self.root / "calls.txt").read_text().splitlines(), ["28"])

    async def test_external_read_only_hint_does_not_authorize_replay(self):
        ident = await self.configured(read_only=True)

        def script(step, body):
            if step == 1:
                return sse_tool_call("load", "tool_search", {"query": "action_28", "limit": 1})
            if step in (2, 3):
                return sse_tool_call(str(step), "mcp__catalog__action_28", {})
            self.assertIn("prior_result_unknown", str(body))
            return sse_text_reply("done", "Inspect the external operation")

        self.gateway.script_fn = script
        await self.run_prompt(ident)
        self.assertEqual((self.root / "calls.txt").read_text().splitlines(), ["28"])

    async def internal_read(self, mode, limit=3):
        from assistant_runtime.platforms.ipc import serve
        from assistant_runtime.core.pi.mcp import server_config

        path = self.root / (mode + "-" + str(limit) + ".sock")
        attempts = []

        async def reply(reader, writer):
            try:
                request = json.loads(await reader.readline())
                if request["command"] == "plugin-tools":
                    value = {
                        "ok": True,
                        "result": {
                            "tools": [
                                {
                                    "name": "observe",
                                    "description": "Observe synthetic data",
                                    "input_schema": {
                                        "type": "object",
                                        "properties": {},
                                        "additionalProperties": False,
                                    },
                                    "output_schema": {"type": "object"},
                                    "annotations": {"readOnlyHint": True, "destructiveHint": False},
                                }
                            ]
                        },
                    }
                else:
                    attempts.append(request)
                    if mode in {"transient", "unknown"} and len(attempts) == 2:
                        value = {"ok": True, "result": {"value": "READ_RECOVERED"}}
                    elif mode in {"unknown", "unknown_exhaust"}:
                        value = {"ok": False, "error": "private-transport-message"}
                    else:
                        value = {
                            "ok": False,
                            "error": "read failure",
                            "tool_outcome": {
                                "code": "provider_file_identity_mismatch"
                                if mode == "permanent"
                                else "provider_network_error",
                                "stage": "return",
                                "dispatch": "dispatched",
                                "outcome": "unknown" if mode == "uncertain" else "failed",
                                "message": "OWNER_SAFE_ERROR",
                                "next_action": "inspect_provider_response"
                                if mode == "permanent"
                                else "retry_read",
                                "retryable": mode != "permanent",
                            },
                        }
                writer.write((json.dumps(value) + "\n").encode())
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()

        listener = await serve(path, reply)
        self.addAsyncCleanup(listener.wait_closed)
        self.addCleanup(listener.close)
        self.client.settings = replace(self.settings, tool_read_attempt_limit=limit)
        ident = await self.session(config={"mcp_servers": server_config(path, plugins=())})
        return ident, attempts

    async def test_internal_reads_recover_without_replaying_original_writes(self):
        for mode in ("transient", "unknown"):
            with self.subTest(mode=mode):
                ident, attempts = await self.internal_read(mode)
                start = len(self.gateway.requests)

                def script(step, body):
                    phase = step - start
                    if phase == 1:
                        return sse_tool_call("load", "tool_search", {"query": "observe", "limit": 1})
                    if phase in (2, 3):
                        if phase == 3:
                            self.assertNotIn("prior_result_unknown", str(body))
                        return sse_tool_call(str(phase), "mcp__extensions__observe", {})
                    self.assertIn("READ_RECOVERED", str(body))
                    return sse_text_reply("done", "Read recovered")

                self.gateway.script_fn = script
                await self.run_prompt(ident)
                self.assertEqual(len(attempts), 2)

    async def test_permanent_read_failure_is_not_sent_again_and_transient_attempts_are_bounded(self):
        for mode, limit in (("permanent", 3), ("exhaust", 3), ("unknown_exhaust", 3), ("exhaust", 1)):
            with self.subTest(mode=mode, limit=limit):
                ident, attempts = await self.internal_read(mode, limit)
                start = len(self.gateway.requests)

                def script(step, body):
                    phase = step - start
                    if phase == 1:
                        return sse_tool_call("load", "tool_search", {"query": "observe", "limit": 1})
                    return sse_tool_call(str(phase), "mcp__extensions__observe", {})

                self.gateway.script_fn = script
                await self.client.request(
                    "run.start",
                    {"session_id": ident, "input": [{"type": "text", "text": "synthetic repeated read"}]},
                )
                run = await self.completed()
                self.assertEqual(run["error"]["code"], "repeated_read_failure", run)
                self.assertEqual(
                    len(attempts), {"permanent": 1, "exhaust": limit, "unknown_exhaust": min(2, limit)}[mode]
                )
                if mode != "unknown_exhaust":
                    self.assertIn("OWNER_SAFE_ERROR", run["error"]["message"])

    async def test_explicit_owner_uncertainty_overrides_even_trusted_read_hint(self):
        ident, attempts = await self.internal_read("uncertain")

        def script(step, body):
            if step == 1:
                return sse_tool_call("load", "tool_search", {"query": "observe", "limit": 1})
            if step in (2, 3):
                return sse_tool_call(str(step), "mcp__extensions__observe", {})
            self.assertIn("prior_result_unknown", str(body))
            return sse_text_reply("done", "Owner receipt requires inspection")

        self.gateway.script_fn = script
        await self.run_prompt(ident)
        self.assertEqual(len(attempts), 1)

    async def test_internal_failure_crosses_ipc_with_field_and_corrected_call_executes_once(self):
        from assistant_runtime.platforms.ipc import serve
        from assistant_runtime.api.tool_calls import invalid_arguments

        path = self.root / "test-control.sock"
        dispatched = []

        async def reply(reader, writer):
            try:
                request = json.loads(await reader.readline())
                if request["command"] == "plugin-tools":
                    value = {
                        "ok": True,
                        "result": {
                            "tools": [
                                {
                                    "name": "submit",
                                    "description": "Synthetic submission",
                                    "input_schema": {
                                        "type": "object",
                                        "properties": {"reason": {"type": "string"}},
                                        "required": ["reason"],
                                    },
                                    "output_schema": {"type": "object"},
                                    "annotations": {"readOnlyHint": False},
                                }
                            ]
                        },
                    }
                elif request["arguments"]["reason"] != "corrected":
                    failure = invalid_arguments("Provide corrected reason", "reason")
                    value = {"ok": False, "error": "short failure", "tool_outcome": failure.outcome.public()}
                else:
                    dispatched.append(request)
                    value = {"ok": True, "result": {"operation_id": "receipt-1", "state": "submitted"}}
                writer.write((json.dumps(value) + "\n").encode())
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()

        server = await serve(path, reply)
        try:
            ident = await self.session(
                config={
                    "allowed_tools": ["mcp__extensions__submit"],
                    "mcp_servers": {
                        "extensions": {
                            "command": sys.executable,
                            "args": ["-m", "assistant_runtime.mcp", "extensions", "--socket", str(path)],
                        }
                    },
                },
                _stable_execution_prompt=True,
            )

            def script(step, body):
                if step == 1:
                    return sse_tool_call("bad", "mcp__extensions__submit", {"reason": "wrong"})
                if step == 2:
                    self.assertIn("invalid_arguments; not_dispatched", str(body))
                    return sse_tool_call("fixed", "mcp__extensions__submit", {"reason": "corrected"})
                self.assertIn("receipt-1", str(body))
                return sse_text_reply("done", "done")

            self.gateway.script_fn = script
            await self.run_prompt(ident)
            self.assertEqual(len(dispatched), 1)
            self.assertEqual(dispatched[0]["execution_session"], ident)
        finally:
            server.close()
            await server.wait_closed()

    async def test_exact_lookup_does_not_reveal_hidden_tools(self):
        ident = await self.configured(tools=["mcp__catalog__action_3"])

        def script(step, body):
            if step == 1:
                return sse_tool_call(
                    "hidden", "tool_search", {"query": "mcp__catalog__action_29", "limit": 1}
                )
            if step == 2:
                self.assertFalse(any(t.get("name", "").startswith("mcp__") for t in body.get("tools", [])))
                self.assertIn("No match in the current ready, authorized catalog", str(body))
                return sse_tool_call("wrong-server", "tool_search", {"query": "missing.action_3", "limit": 1})
            if step == 3:
                self.assertFalse(any(t.get("name", "").startswith("mcp__") for t in body.get("tools", [])))
                self.assertIn("No match in the current ready, authorized catalog", str(body))
                return sse_tool_call(
                    "allowed", "tool_search", {"query": "mcp__catalog__action_3", "limit": 1}
                )
            return sse_text_reply("done", "done")

        self.gateway.script_fn = script
        await self.run_prompt(ident)
        self.assertEqual(len(self.gateway.requests), 4)

    async def test_native_batch_parallelizes_internal_reads_but_serializes_writes(self):
        from assistant_runtime.platforms.ipc import serve

        for read_only in (True, False):
            with self.subTest(read_only=read_only):
                path = self.root / f"batch-{read_only}.sock"
                activity = []
                entered = asyncio.Event()

                async def reply(reader, writer):
                    try:
                        request = json.loads(await reader.readline())
                        if request["command"] == "plugin-tools":
                            result = {
                                "tools": [
                                    {
                                        "name": "sample",
                                        "description": "Synthetic operation",
                                        "input_schema": {
                                            "type": "object",
                                            "properties": {"id": {"type": "integer"}},
                                            "required": ["id"],
                                        },
                                        "output_schema": {"type": "object"},
                                        "annotations": {"readOnlyHint": read_only},
                                    }
                                ]
                            }
                        else:
                            ident = request["arguments"]["id"]
                            activity.append(("start", ident))
                            if ident == 2:
                                entered.set()
                            if read_only:
                                await asyncio.wait_for(entered.wait(), 5)
                            else:
                                await asyncio.sleep(0.05)
                            activity.append(("end", ident))
                            result = {"verified": ident}
                        writer.write((json.dumps({"ok": True, "result": result}) + "\n").encode())
                        await writer.drain()
                    finally:
                        writer.close()
                        await writer.wait_closed()

                server = await serve(path, reply)
                try:
                    ident = await self.session(
                        config={
                            "allowed_tools": ["mcp__extensions__sample"],
                            "mcp_servers": {
                                "extensions": {
                                    "command": sys.executable,
                                    "args": [
                                        "-m",
                                        "assistant_runtime.mcp",
                                        "extensions",
                                        "--socket",
                                        str(path),
                                    ],
                                }
                            },
                        },
                        _stable_execution_prompt=True,
                    )
                    self.gateway.step = 0
                    self.gateway.script_fn = lambda step, body: (
                        sse_tool_batch([(str(i), "mcp__extensions__sample", {"id": i}) for i in (1, 2)])
                        if step == 1
                        else sse_text_reply("done", "verified")
                    )
                    await self.run_prompt(ident)
                    if read_only:
                        self.assertEqual(activity[:2], [("start", 1), ("start", 2)])
                        self.assertEqual(sorted(activity[2:]), [("end", 1), ("end", 2)])
                    else:
                        self.assertEqual(activity, [("start", 1), ("end", 1), ("start", 2), ("end", 2)])
                finally:
                    server.close()
                    await server.wait_closed()

    async def test_live_session_keeps_bounded_warm_tools_between_user_turns(self):
        ident = await self.configured(limit=1)

        def script(step, body):
            names = [t.get("name", "") for t in body.get("tools", [])]
            self.assertLessEqual(sum(n.startswith("mcp__") for n in names), 1)
            if step == 1:
                return sse_tool_call("load", "tool_search", {"query": "action_3", "limit": 1})
            if step in (2, 4):
                self.assertIn("mcp__catalog__action_3", names)
                return sse_tool_call("call-" + str(step), "mcp__catalog__action_3", {})
            return sse_text_reply("done-" + str(step), "done")

        self.gateway.script_fn = script
        await self.run_prompt(ident)
        await self.run_prompt(ident)
        self.assertEqual(len(self.gateway.requests), 5)
        self.assertEqual((self.root / "calls.txt").read_text().splitlines(), ["3", "3"])

    async def test_many_searches_remain_bounded_and_evicted_tool_can_be_reloaded(self):
        ident = await self.configured()

        def script(step, body):
            names = [t.get("name", "") for t in body.get("tools", [])]
            self.assertLessEqual(sum(n.startswith("mcp__") for n in names), 2)
            # Historical schema patches must not retain every previously loaded definition.
            self.assertLessEqual(json.dumps(body).count("SCHEMA_SENTINEL"), 4)
            if step <= 8:
                return sse_tool_call(str(step), "tool_search", {"query": f"action_{step}", "limit": 1})
            if step == 9:
                return sse_tool_call("reload", "tool_search", {"query": "action_1", "limit": 1})
            if step == 10:
                self.assertIn("mcp__catalog__action_1", names)
                return sse_tool_call("call", "mcp__catalog__action_1", {})
            return sse_text_reply("done", "done")

        self.gateway.script_fn = script
        await self.run_prompt(ident)
        self.assertEqual(len(self.gateway.requests), 11)
        self.assertIn("verified:1", json.dumps(self.gateway.requests[-1]))
        self.assertFalse(
            any(t.get("name", "").startswith("mcp__") for t in self.gateway.requests[0].get("tools", []))
        )
        self.assertTrue(
            any(
                len(r.get("tools", [])) > len(self.gateway.requests[0].get("tools", []))
                for r in self.gateway.requests[1:]
            )
        )

    async def test_multiple_names_are_not_an_ambiguity(self):
        ident = await self.configured()

        def script(step, body):
            if step == 1:
                return sse_tool_call("two", "tool_search", {"query": "action_3 action_4", "limit": 2})
            names = [t["name"] for t in body.get("tools", [])]
            self.assertIn("mcp__catalog__action_3", names)
            self.assertIn("mcp__catalog__action_4", names)
            self.assertNotIn("ambiguous", str(body))
            return sse_text_reply("done", "done")

        self.gateway.script_fn = script
        await self.run_prompt(ident)
        self.assertFalse((self.root / "calls.txt").exists())

    async def test_chinese_query_has_a_bounded_namespace_recovery(self):
        ident = await self.configured()

        def script(step, body):
            if step == 1:
                return sse_tool_call("chinese", "tool_search", {"query": "查找目录工具", "limit": 2})
            if step == 2:
                self.assertIn("browse a namespace: catalog", str(body))
                return sse_tool_call(
                    "source", "tool_search", {"query": "", "namespace": "catalog", "limit": 2}
                )
            self.assertEqual(sum(t["name"].startswith("mcp__") for t in body.get("tools", [])), 2)
            return sse_text_reply("done", "done")

        self.gateway.script_fn = script
        await self.run_prompt(ident)

    async def test_repeated_invalid_arguments_stop_without_dispatch(self):
        ident = await self.configured()
        self.gateway.script_fn = lambda step, body: sse_tool_call(
            str(step), "tool_search", {"query": "action", "limit": 999}
        )
        await self.client.request(
            "run.start", {"session_id": ident, "input": [{"type": "text", "text": "synthetic"}]}
        )
        result = await self.completed()
        self.assertEqual(result["status"], "failed", result)
        self.assertEqual(len(self.gateway.requests), 3)
        self.assertFalse((self.root / "calls.txt").exists())

    async def test_single_large_definition_is_intact_and_evicted_tool_stays_callable(self):
        ident = await self.configured(limit=3)
        self.client.settings = replace(self.client.settings, tool_schema_max_bytes=1024)

        def script(step, body):
            names = [t["name"] for t in body.get("tools", []) if t["name"].startswith("mcp__")]
            self.assertLessEqual(len(names), 1)
            if step == 1:
                return sse_tool_call("one", "tool_search", {"query": "action_3", "limit": 1})
            if step == 2:
                self.assertIn("mcp__catalog__action_3", names)
                return sse_tool_call("two", "tool_search", {"query": "action_4", "limit": 1})
            if step == 3:
                self.assertIn("mcp__catalog__action_4", names)
                return sse_tool_call("old", "mcp__catalog__action_3", {})
            self.assertIn("verified:3", str(body))
            return sse_text_reply("done", "done")

        self.gateway.script_fn = script
        await self.run_prompt(ident)
        self.assertEqual((self.root / "calls.txt").read_text().splitlines(), ["3"])

    async def test_small_explicit_execution_loads_before_first_model_request(self):
        ident = await self.configured(tools=["mcp__catalog__action_3"], stable=True)

        def script(step, body):
            names = [t["name"] for t in body.get("tools", [])]
            self.assertIn("mcp__catalog__action_3", names)
            self.assertNotIn("mcp__catalog__action_4", names)
            if step == 1:
                return sse_tool_call("business", "mcp__catalog__action_3", {})
            return sse_text_reply("done", "verified")

        self.gateway.script_fn = script
        await self.run_prompt(ident)
        self.assertEqual(len(self.gateway.requests), 2)
        self.assertEqual(self.gateway.requests[0]["tools"], self.gateway.requests[1]["tools"])
        self.assertEqual((self.root / "calls.txt").read_text().splitlines(), ["3"])
        await self.client.close()
        await self.client.start()
        await self.client.request("session.resume", {"session_id": ident})
        self.gateway.script_fn = lambda step, body: sse_text_reply("resume", "done")
        await self.run_prompt(ident)
        self.assertIn("mcp__catalog__action_3", [t["name"] for t in self.gateway.requests[-1]["tools"]])

    async def test_original_hyphenated_identity_uses_the_same_permission_and_schema(self):
        script = self.root / "hyphenated.py"
        script.write_text(
            "from fastmcp import FastMCP\nm=FastMCP('names')\n@m.tool(name='read-item')\ndef read_item(): return 'verified'\nm.run(transport='stdio',show_banner=False)\n"
        )
        ident = await self.session(
            config={
                "mcp_servers": {
                    "hyphenated-source": {
                        "command": sys.executable,
                        "args": [str(script)],
                        "enabled": True,
                        "enabled_tools": ["read-item"],
                        "default_tools_approval_mode": "approve",
                    }
                }
            }
        )

        def respond(step, body):
            if step == 1:
                return sse_tool_call("find", "tool_search", {"query": "hyphenated-source.read-item"})
            if step == 2:
                self.assertIn(
                    "mcp__hyphenated_source__read_item", [t.get("name") for t in body.get("tools", [])]
                )
                return sse_tool_call("read", "mcp__hyphenated_source__read_item", {})
            self.assertIn("verified", str(body))
            return sse_text_reply("done", "verified")

        self.gateway.script_fn = respond
        await self.run_prompt(ident)
        self.assertEqual(len(self.gateway.requests), 3)

    async def test_scoped_cold_search_does_not_wait_for_an_unrelated_server(self):
        release = self.root / "release-slow"
        slow = self.root / "slow.py"
        slow.write_text(
            "import time\nfrom pathlib import Path\n"
            f"while not Path({str(release)!r}).exists(): time.sleep(.05)\n"
            "from fastmcp import FastMCP\nm=FastMCP('slow')\nm.run(transport='stdio',show_banner=False)\n"
        )
        fast = self.root / "fast.py"
        fast.write_text(
            "from fastmcp import FastMCP\nm=FastMCP('fast')\n@m.tool\ndef query(): return 'ok'\nm.run(transport='stdio',show_banner=False)\n"
        )
        ident = await self.session(
            config={
                "mcp_servers": {
                    name: {"command": sys.executable, "args": [str(path)], "enabled": True}
                    for name, path in (("fast", fast), ("slow", slow))
                }
            }
        )

        def respond(step, body):
            if step == 1:
                return sse_tool_call("find", "tool_search", {"query": "", "namespace": "fast"})
            self.assertIn("mcp__fast__query", [t.get("name") for t in body.get("tools", [])])
            release.touch()
            return sse_text_reply("done", "ready")

        self.gateway.script_fn = respond
        try:
            await asyncio.wait_for(self.run_prompt(ident), 20)
        finally:
            release.touch()

    async def test_preload_over_count_or_schema_bound_stays_deferred(self):
        for huge in (False, True):
            with self.subTest(huge=huge):
                tools = ["mcp__catalog__action_" + str(i) for i in range(1 if huge else 5)]
                ident = await self.configured(tools=tools, limit=8, stable=True)
                if huge:
                    path = self.root / "catalog.py"
                    path.write_text(path.read_text().replace("*80", "*2000"))
                self.gateway.script_fn = lambda step, body: sse_text_reply("done", "done")
                await self.run_prompt(ident)
                self.assertNotIn("SCHEMA_SENTINEL", str(self.gateway.requests[-1]))
                self.assertFalse(
                    any(t["name"].startswith("mcp__") for t in self.gateway.requests[-1]["tools"])
                )
                await self.client.request("session.close", {"session_id": ident})

    async def test_preload_keeps_hidden_tools_and_other_scope_unavailable(self):
        ident = await self.configured(tools=["mcp__catalog__action_3"], stable=True)

        def script(step, body):
            names = [t["name"] for t in body.get("tools", [])]
            self.assertNotIn("mcp__catalog__action_4", names)
            if step == 1:
                return sse_tool_call("forged", "mcp__catalog__action_4", {})
            self.assertIn("not found", str(body))
            return sse_text_reply("done", "done")

        self.gateway.script_fn = script
        await self.run_prompt(ident)
        self.assertFalse((self.root / "calls.txt").exists())


class MappingTest(unittest.TestCase):
    def test_service_reject_cannot_be_overridden_by_individual_allow(self):
        from assistant_runtime.core.pi.mcp import to_pi_mcp_json

        config = to_pi_mcp_json(
            {
                "sample": {
                    "command": "python",
                    "args": ["server.py"],
                    "enabled_tools": ["read", "write", "remove"],
                    "disabled_tools": ["remove"],
                    "default_tools_approval_mode": "reject",
                    "tools": {
                        "read": {"approval_mode": "approve"},
                        "write": {"approval_mode": "reject"},
                    },
                }
            }
        )["mcpServers"]["sample"]
        self.assertEqual(config["exposure"], "hidden")
        self.assertEqual(
            config["toolExposure"],
            {"remove": "hidden", "write": "hidden"},
        )
        self.assertEqual(config["args"], ["server.py"])

    def test_eager_mapping_never_promotes_a_denied_or_unselected_tool(self):
        from assistant_runtime.core.pi.mcp import to_pi_mcp_json

        result = to_pi_mcp_json(
            {
                "sample": {
                    "command": "python",
                    "enabled_tools": ["read", "remove"],
                    "disabled_tools": ["remove"],
                }
            },
            preload=True,
            allowed=["mcp__sample__read", "mcp__sample__remove"],
            limit=3,
        )
        self.assertEqual(
            result["mcpServers"]["sample"]["toolExposure"], {"read": "direct", "remove": "hidden"}
        )
