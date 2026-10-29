"""Native Pi search/loading, bounded declarations and unchanged authorization."""

import json
from dataclasses import replace
import sys
import unittest

from pi_gateway import sse_text_reply, sse_tool_call
import test_pi_skeleton as fixture


class DiscoveryTest(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.PiSkeletonTest.asyncSetUp
    asyncTearDown = fixture.PiSkeletonTest.asyncTearDown
    session = fixture.PiSkeletonTest.session
    completed = fixture.PiSkeletonTest.completed

    async def configured(self, *, tools=None, limit=2, second=False, stable=False):
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
            " m.tool(name='action_'+str(i),description='Catalog action '+str(i)+'\\nSCHEMA_SENTINEL '+('catalog operation '+str(i)+' ')*80)(make(i))\n"
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
                self.assertIn("Search limit must", str(body))
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

    async def test_unloaded_call_is_prepared_without_replaying_business_operation(self):
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
                self.assertIn("this call did not execute", str(body))
                self.assertNotIn("verified:3", str(body))
                return sse_tool_call("call", "mcp__catalog__action_3", {})
            return sse_text_reply("done", "done")

        self.gateway.script_fn = script
        await self.run_prompt(ident)
        self.assertEqual(len(self.gateway.requests), 4)
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

    async def test_exact_lookup_does_not_reveal_hidden_tools(self):
        ident = await self.configured(tools=["mcp__catalog__action_3"])

        def script(step, body):
            if step == 1:
                return sse_tool_call(
                    "hidden", "tool_search", {"query": "mcp__catalog__action_29", "limit": 1}
                )
            if step == 2:
                self.assertFalse(any(t.get("name", "").startswith("mcp__") for t in body.get("tools", [])))
                self.assertIn("No available tool matches", str(body))
                return sse_tool_call("wrong-server", "tool_search", {"query": "missing.action_3", "limit": 1})
            if step == 3:
                self.assertFalse(any(t.get("name", "").startswith("mcp__") for t in body.get("tools", [])))
                self.assertIn("No available tool matches", str(body))
                return sse_tool_call(
                    "allowed", "tool_search", {"query": "mcp__catalog__action_3", "limit": 1}
                )
            return sse_text_reply("done", "done")

        self.gateway.script_fn = script
        await self.run_prompt(ident)
        self.assertEqual(len(self.gateway.requests), 4)

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
    def test_allow_deny_and_approval_are_applied_before_discovery(self):
        from assistant_runtime.core.pi.mcp import to_pi_mcp_json

        config = to_pi_mcp_json(
            {
                "sample": {
                    "command": "python",
                    "args": ["server.py"],
                    "enabled_tools": ["read", "write", "remove", "confirm"],
                    "disabled_tools": ["remove"],
                    "default_tools_approval_mode": "reject",
                    "tools": {
                        "read": {"approval_mode": "approve"},
                        "write": {"approval_mode": "reject"},
                        "confirm": {"approval_mode": "prompt"},
                    },
                }
            }
        )["mcpServers"]["sample"]
        self.assertEqual(config["exposure"], "hidden")
        self.assertEqual(
            config["toolExposure"],
            {"read": "deferred", "confirm": "deferred", "remove": "hidden", "write": "hidden"},
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
