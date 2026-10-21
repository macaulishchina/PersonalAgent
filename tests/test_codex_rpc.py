"""Bounded stdio replies and startup cleanup; no model or hardware calls."""

import asyncio
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from assistant_runtime.conversation.service import Conversation
from assistant_runtime.plugins.codex.backend import CodexBackend
from assistant_runtime.plugins.codex.rpc import CodexClient, RpcError


class CodexRpcTest(unittest.IsolatedAsyncioTestCase):
    async def test_resume_sized_reply_over_old_limit_through_real_stdio(self):
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "fake_codex"
            script.write_text(
                f"#!{sys.executable}\n"
                "import json,sys\n"
                "for line in sys.stdin:\n"
                " m=json.loads(line)\n"
                " if 'id' not in m: continue\n"
                " result={} if m['method']=='initialize' else {'payload':'x'*(17*1024*1024)}\n"
                " print(json.dumps({'id':m['id'],'result':result}),flush=True)\n"
            )
            script.chmod(0o700)
            client = CodexClient(str(script), directory)
            try:
                await client.start()
                result = await client.request("thread/read", {}, timeout=10)
                self.assertEqual(len(result["payload"]), 17 * 1024 * 1024)
            finally:
                await client.close()

    async def test_frame_failure_is_explicit_without_logging_contents_or_retry(self):
        for payload, reason in ((b"x" * 2048, "frame limit"), (b"private-invalid-json\n", "invalid JSON")):
            with self.subTest(reason=reason):
                client = CodexClient()
                reader = asyncio.StreamReader(limit=1024)
                client.process = SimpleNamespace(stdout=reader)
                pending = asyncio.get_running_loop().create_future()
                client.pending[1] = pending
                reader.feed_data(payload)
                reader.feed_eof()
                with patch("assistant_runtime.plugins.codex.rpc.MAX_FRAME_BYTES", 1024):
                    with self.assertLogs("assistant_runtime.plugins.codex.rpc", level="WARNING") as logs:
                        await client._read()
                with self.assertRaisesRegex(RpcError, reason):
                    await pending
                self.assertNotIn("private-invalid-json", " ".join(logs.output))
                self.assertEqual((await client.events.get())["method"], "local/disconnected")

    async def test_voice_cleanup_before_provider_start_does_not_touch_history(self):
        backend = CodexBackend(
            SimpleNamespace(codex_binary="codex", workspace="/tmp"), Mock(), Mock(), Mock()
        )
        self.assertIsNone(backend.session_id)
        conversation = Conversation.__new__(Conversation)
        conversation.host = SimpleNamespace(backend=backend)
        conversation.feedback = Mock()
        conversation.media_event({"kind": "closed"}, 0)
        conversation.media_event({"kind": "closed"}, 1)
        conversation.feedback.close.assert_not_called()
