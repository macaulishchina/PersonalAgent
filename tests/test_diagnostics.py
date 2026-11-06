import json
import logging
from pathlib import Path
import tempfile
import unittest
from assistant_runtime.app.diagnostics import DiagnosticFormatter, DiagnosticHandler, configure


class DiagnosticsTest(unittest.TestCase):
    def test_error_chain_redaction_and_no_locals(self):
        try:
            try:
                raise ValueError(
                    "Bearer synthetic-bearer-value password=synthetic-password https://host/?key=secret"
                )
            except ValueError as cause:
                raise RuntimeError("local parsing failed") from cause
        except RuntimeError:
            import sys

            record = logging.LogRecord("test", logging.ERROR, "", 1, "model.failed", (), sys.exc_info())
        record.diagnostic = {
            "prompt": "never-output",
            "api_key": "private-key",
            "native_error": "TypeError: invalid stream",
            "response_id": "resp_synthetic",
        }
        value = DiagnosticFormatter("business").format(record)
        data = json.loads(value)
        self.assertEqual([e["type"] for e in data["errors"]], ["RuntimeError", "ValueError"])
        self.assertEqual(data["fields"]["response_id"], "resp_synthetic")
        for secret in ["synthetic-bearer-value", "synthetic-password", "never-output", "private-key"]:
            self.assertNotIn(secret, value)

    def test_file_rotation_and_debug_level(self):
        with tempfile.TemporaryDirectory() as temp:
            handler = DiagnosticHandler(
                Path(temp) / "events.jsonl", maxBytes=1024, backupCount=2, encoding="utf-8"
            )
            handler.setFormatter(DiagnosticFormatter("business"))
            logger = logging.Logger("test", logging.DEBUG)
            logger.addHandler(handler)
            for _ in range(40):
                logger.debug("stage", extra={"diagnostic": {"reason": "x" * 400}})
            handler.close()
            self.assertLessEqual(len(list(Path(temp).glob("events.jsonl*"))), 3)
            self.assertIn('"level": "DEBUG"', (Path(temp) / "events.jsonl").read_text())

    def test_invalid_level_rejected(self):
        with self.assertRaises(ValueError):
            configure("test", level="RAW")


class WorkerDiagnosticTest(unittest.IsolatedAsyncioTestCase):
    async def test_stderr_drain_is_bounded_and_secret_redacted(self):
        import asyncio
        from types import SimpleNamespace
        from assistant_runtime.core.pi.accounts import AccountWorker

        worker = AccountWorker(Path("."), Path("."), {}, credential_value="synthetic-worker-key")
        stderr = asyncio.StreamReader()
        stderr.feed_data(
            (
                "TypeError: failed synthetic-worker-key\n"
                + "source excerpt private payload\n"
                + "Error: repeated\n" * 40
            ).encode()
        )
        stderr.feed_eof()
        worker.process = SimpleNamespace(stderr=stderr)
        with self.assertLogs("assistant_runtime.core.pi.accounts", level="WARNING") as logs:
            await worker._read_stderr()
        self.assertLessEqual(len(logs.records), 33)
        facts = [getattr(r, "diagnostic", {}) for r in logs.records]
        self.assertNotIn("synthetic-worker-key", str(facts))
        self.assertNotIn("private payload", str(facts))
        self.assertIn("TypeError", str(facts))


class StartupDiagnosticTest(unittest.IsolatedAsyncioTestCase):
    async def test_validation_failure_keeps_private_cause_and_public_status(self):
        from unittest.mock import patch
        from assistant_runtime.app.management import BusinessProcess
        from assistant_runtime.core.configuration.settings import ManagementSettings

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            entry = root / "config.toml"
            entry.write_text("plugins=[]\n")
            business = BusinessProcess(ManagementSettings(root, entry))
            with patch.object(
                business.files, "validate_all", side_effect=ValueError("synthetic validation failure")
            ):
                with self.assertLogs("assistant_runtime.app.management", level="ERROR") as logs:
                    await business.start()
            self.assertEqual(business.outcome, "failed")
            record = next(r for r in logs.records if r.msg == "business.start_failed")
            self.assertEqual(record.diagnostic["stage"], "configuration")
            self.assertIs(record.exc_info[0], ValueError)
            self.assertNotIn("synthetic validation failure", business.error)
