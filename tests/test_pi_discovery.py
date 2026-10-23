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

    async def configured(self, *, tools=None, limit=2):
        self.client.settings = replace(self.settings, max_loaded_tools=limit)
        script = self.root / "catalog.py"
        script.write_text(
            "from fastmcp import FastMCP\nm=FastMCP('catalog')\n"
            "for i in range(30):\n"
            " def make(i):\n"
            "  def action(): return 'verified:'+str(i)\n"
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
        return await self.session(config=config)

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
