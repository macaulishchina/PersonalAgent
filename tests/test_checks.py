"""A fast gate must still discover every test, fail closed and clean up its children."""

import asyncio
import contextlib
import io
import os
from pathlib import Path
import sys
import tempfile
import unittest

from scripts.check import execute, groups, run_tests, select


class SelectionTest(unittest.TestCase):
    def test_new_nested_module_runs_in_full_and_scoped_checks_without_manifest_edit(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "tests/new_domain").mkdir(parents=True)
            (root / "tests/test_integrations.py").touch()
            (root / "tests/new_domain/test_new.py").touch()
            self.assertEqual(set(select(root)), {"test_integrations", "new_domain.test_new"})
            self.assertIn("new_domain.test_new", groups(root)["core"])
            selected = select(root, ["codex"], ["test_integrations.Example.test_one"])
            self.assertEqual(selected, {"test_integrations": ["test_integrations"]})
            self.assertEqual(
                select(root, targets=["new_domain.test_new.Example"]),
                {"new_domain.test_new": ["new_domain.test_new.Example"]},
            )
            for target in ("test_missing", "test_integrations;echo", "../test_integrations"):
                with self.assertRaises(ValueError):
                    select(root, targets=[target])

    def test_empty_suite_never_reports_success(self):
        with tempfile.TemporaryDirectory() as folder, self.assertRaisesRegex(ValueError, "No test modules"):
            select(Path(folder))


class ProcessTest(unittest.IsolatedAsyncioTestCase):
    async def test_cancel_stops_a_new_session_worker_without_signaling_unrelated_process(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            unrelated = await asyncio.create_subprocess_exec(
                sys.executable, "-c", "import time; time.sleep(60)", start_new_session=True
            )
            child = (
                "import os,pathlib,time; pathlib.Path('child.tmp').write_text(str(os.getpid())); "
                "pathlib.Path('child.tmp').replace('child'); time.sleep(60)"
            )
            parent = f"import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',{child!r}],start_new_session=True); time.sleep(60)"
            task = asyncio.create_task(execute("worker-owner", [sys.executable, "-c", parent], root, 10))
            try:
                async with asyncio.timeout(5):
                    while not (root / "child").exists():
                        await asyncio.sleep(0.01)
                pid = int((root / "child").read_text())
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                status = Path(f"/proc/{pid}/stat")
                self.assertTrue(not status.exists() or status.read_text().rsplit(")", 1)[1].split()[0] == "Z")
                self.assertIsNone(unrelated.returncode)
            finally:
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                unrelated.terminate()
                await unrelated.wait()

    async def test_parallel_modules_use_distinct_processes_and_failures_propagate(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "tests").mkdir()
            for name, peer in (("first", "second"), ("second", "first")):
                (root / f"tests/test_{name}.py").write_text(
                    "import os, pathlib, time, unittest\n"
                    "class Isolated(unittest.TestCase):\n"
                    " def test_peer(self):\n"
                    f"  pathlib.Path('{name}').write_text(str(os.getpid()))\n"
                    "  deadline=time.monotonic()+5\n"
                    f"  while not pathlib.Path('{peer}').exists() and time.monotonic()<deadline: time.sleep(.01)\n"
                    f"  self.assertTrue(pathlib.Path('{peer}').exists(), 'peer did not run concurrently')\n"
                    f"  self.assertNotEqual(pathlib.Path('{peer}').read_text(),str(os.getpid()))\n"
                )
            with contextlib.redirect_stdout(io.StringIO()):
                results = await run_tests(root, select(root), 2, 8)
            self.assertTrue(all(r.code == 0 and r.count == 1 for r in results))
            (root / "tests/test_failure.py").write_text(
                "import unittest\nclass Broken(unittest.TestCase):\n def test_failure(self): self.fail('expected failure')\n"
            )
            with contextlib.redirect_stdout(io.StringIO()):
                results = await run_tests(root, select(root, targets=["test_failure"]), 1, 8)
            self.assertNotEqual(results[0].code, 0)
            self.assertIn("expected failure", results[0].output)

    async def test_timeout_and_cancellation_stop_the_owned_command(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            code = (
                "import os,pathlib,time; pathlib.Path('pid.tmp').write_text(str(os.getpid())); "
                "pathlib.Path('pid.tmp').replace('pid'); time.sleep(60)"
            )
            result = await execute("blocked", [sys.executable, "-c", code], root, 0.5)
            self.assertEqual(result.code, 124)
            pid = int((root / "pid").read_text())
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)
            (root / "pid").unlink()
            task = asyncio.create_task(execute("canceled", [sys.executable, "-c", code], root, 10))
            async with asyncio.timeout(5):
                while not (root / "pid").exists():
                    await asyncio.sleep(0.01)
            pid = int((root / "pid").read_text())
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)
