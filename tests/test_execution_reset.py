"""State reset is explicit, backed up, idempotent and preserves memory evidence."""

import fcntl
from pathlib import Path
import sqlite3
import tempfile
import unittest
from assistant_runtime.app.store import Store
from assistant_runtime.core.tasks.repository import TaskRepository
from scripts.reset_execution_state import reset


class ResetTest(unittest.TestCase):
    def test_preview_backup_reset_and_repeat_preserve_business_data(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            db = Store(root / "state/assistant.sqlite3")
            TaskRepository(db.db)
            db.transcripts.append("user", "memory source")
            db.executions.create_task("old", "old execution")
            db.set("thread_id", "old-executor")
            db.db.execute("CREATE TABLE media_evidence(id TEXT)")
            db.db.execute("INSERT INTO media_evidence VALUES('keep')")
            db.db.commit()
            self.assertEqual(reset(root)["state"], "preview")
            self.assertEqual(len(db.executions.recent()), 1)
            with self.assertRaises(ValueError):
                reset(root, apply=True)
            result = reset(root, apply=True, confirmed_stopped=True)
            self.assertEqual(result["state"], "reset")
            self.assertEqual(db.executions.recent(), [])
            self.assertEqual(db.transcripts.last_id(), 1)
            self.assertEqual(db.db.execute("SELECT id FROM media_evidence").fetchone()[0], "keep")
            backup = sqlite3.connect(Path(result["backup"]) / "assistant.sqlite3")
            self.assertEqual(backup.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 1)
            backup.close()
            self.assertEqual(reset(root, apply=True, confirmed_stopped=True)["state"], "already_reset")
            db.db.close()

    def test_apply_refuses_running_service(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            db = Store(root / "state/assistant.sqlite3")
            with (root / "state/service.lock").open("w") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with self.assertRaisesRegex(RuntimeError, "Stop the assistant"):
                    reset(root, apply=True, confirmed_stopped=True)
            db.db.close()
