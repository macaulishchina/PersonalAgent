"""Account UI uses native operator protocol only; no real credentials or network."""

import asyncio
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from assistant_runtime.core.configuration.accounts import Accounts
from assistant_runtime.core.configuration.settings import ManagementSettings


class AccountTests(unittest.IsolatedAsyncioTestCase):
    async def test_login_prompt_answer_cancel_and_cleanup_do_not_echo_credentials(self):
        sent = []
        waiting = asyncio.Event()
        closed = []

        class Worker:
            async def events(self, action):
                self_action = action
                if self_action != "login":
                    raise AssertionError(action)
                yield {
                    "id": "call",
                    "event": {
                        "type": "auth",
                        "data": {"url": "https://auth.example/authorize", "message": "authorize"},
                    },
                }
                yield {
                    "id": "call",
                    "event": {
                        "type": "prompt",
                        "prompt_id": "prompt",
                        "data": {"type": "manual_code", "message": "code"},
                    },
                }
                waiting.set()
                await asyncio.Event().wait()

            async def send(self, value):
                sent.append(value)

        class Native:
            def __init__(self, *a, **kw):
                pass

            async def account(self, name):
                return Worker()

            async def close(self):
                closed.append(True)

        service = Accounts(SimpleNamespace())
        service.configured = lambda: (
            SimpleNamespace(root=Path("/tmp/synthetic"), pi_binary="pi"),
            SimpleNamespace(accounts={"subscription": {"provider": "openai", "auth": "oauth"}}),
        )
        with patch("assistant_runtime.core.configuration.accounts.NativeAccounts", Native):
            with self.assertRaises(ValueError):
                await service.action("subscription", "stream", True)
            with self.assertRaises(ValueError):
                await service.action("subscription", "login", False)
            result = await service.action("subscription", "login", True)
            await asyncio.wait_for(waiting.wait(), 1)
            inventory = service.public()
            self.assertEqual(inventory["flows"][0]["prompt"]["id"], "prompt")
            await service.answer(result["id"], "prompt", "synthetic-private-code")
            self.assertNotIn("synthetic-private-code", str(service.public()))
            self.assertEqual(sent[0]["value"], "synthetic-private-code")
            with self.assertRaises(ValueError):
                await service.answer(result["id"], "prompt", "again")
            await service.cancel(result["id"])
            self.assertEqual(service.public()["flows"][0]["state"], "cancelled")
            self.assertNotIn("authorization_url", service.public()["flows"][0])
            self.assertTrue(closed)
            await service.close()

    def test_proxy_requires_explicit_networks_and_origin(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = root / "management.toml"
            path.write_text(
                f'root="{root.as_posix()}"\nentry="{root.as_posix()}/config.toml"\npublic_origin="https://settings.example"\n'
            )
            with self.assertRaises(ValueError):
                ManagementSettings.load(path)
            path.write_text(path.read_text() + 'trusted_proxies=["127.0.0.1/32"]\n')
            self.assertEqual(ManagementSettings.load(path).public_origin, "https://settings.example")
