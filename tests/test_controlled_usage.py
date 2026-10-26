"""Round and cumulative ceilings remain in the shared durable ledger."""

import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch
import time
from assistant_runtime.core.ai_usage.service import UsageService
from assistant_runtime.api.ai_usage import UsageScope, UsageError


class ControlledUsageTest(unittest.IsolatedAsyncioTestCase):
    async def test_round_limit_does_not_reset_scope_or_poison_next_round(self):
        with tempfile.TemporaryDirectory() as folder:
            usage = UsageService(Path(folder))
            await usage.start()
            try:
                await usage.controlled_budget("analysis", "", {"max_requests": 2})
                await usage.controlled_budget("analysis", "one", {"max_requests": 1})

                async def admit(caller):
                    return await usage.admit(
                        UsageScope("analysis", caller),
                        service="execution",
                        provider="p",
                        bounds={"input_tokens": 100, "output_tokens": 50},
                    )

                one = await admit("one")
                await usage.settle(
                    one["id"], state="completed", usage={"input_tokens": 10, "output_tokens": 5}
                )
                # Pruning must not refresh per-round or aggregate ceilings.
                with patch(
                    "assistant_runtime.core.ai_usage.ledger.time.time", return_value=time.time() + 400 * 86400
                ):
                    await usage.call("prune")
                with self.assertRaisesRegex(UsageError, "round:max_requests"):
                    await admit("one")
                two = await admit("two")
                await usage.settle(two["id"], state="unknown")
                with self.assertRaisesRegex(UsageError, "scope:max_requests"):
                    await admit("new-id")
                with self.assertRaises(ValueError):
                    await usage.controlled_budget("analysis", "", {"max_requests": 3})
                self.assertEqual((await usage.snapshot("analysis"))["requests"], 2)
            finally:
                await usage.close()

    async def test_unknown_cost_is_not_zero_and_shared_reservations_survive_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            usage = UsageService(Path(folder))
            await usage.start()
            try:
                await usage.controlled_budget("cost", "", {"max_cost_cny": 1})
                with self.assertRaisesRegex(UsageError, "unaccountable"):
                    await usage.admit(UsageScope("cost", "one"), service="execution", provider="unpriced")
                await usage.controlled_budget("tokens", "", {"max_output_tokens": 100})
                t = await usage.admit(
                    UsageScope("tokens", "one"),
                    service="execution",
                    provider="p",
                    bounds={"output_tokens": 80},
                )
                await usage.settle(t["id"], state="unknown")
                await usage.close()
                usage = UsageService(Path(folder))
                await usage.start()
                with self.assertRaisesRegex(UsageError, "scope:max_output_tokens"):
                    await usage.admit(
                        UsageScope("tokens", "retry"),
                        service="execution",
                        provider="p",
                        bounds={"output_tokens": 21},
                    )
            finally:
                await usage.close()
