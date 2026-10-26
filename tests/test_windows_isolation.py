"""Native fixed-command LPAC acceptance; never certifies arbitrary build tools."""

import os
import asyncio
from pathlib import Path
import tempfile
import unittest
import uuid


@unittest.skipUnless(os.name == "nt", "Windows LPAC backend")
class IsolationTest(unittest.IsolatedAsyncioTestCase):
    async def test_fixed_check_isolates_files_and_descendants(self):
        from assistant_runtime.platforms.windows_isolation import IsolatedCheck
        from assistant_runtime.api.system import system

        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            system.private(base, directory=True)
            root = base / "workspace"
            root.mkdir()
            for name in ("work", "source", "checks", "runs"):
                (root / name).mkdir()
            output = root / "runs" / uuid.uuid4().hex
            output.mkdir()
            (base / "secret.txt").write_text("SYNTHETIC_SECRET", encoding="utf-8")
            (root / "source/material.txt").write_text("MATERIAL", encoding="utf-8")
            script = root / "checks/fixed.cmd"
            script.write_bytes(
                b'@echo off\r\necho GOOD>allowed.txt\r\ntype ..\\source\\material.txt >read.txt\r\ntype ..\\..\\secret.txt >denied.txt 2>nul\r\necho BAD>>..\\source\\material.txt 2>nul\r\ncmd /d /c "echo CHILD>child.txt & type ..\\..\\secret.txt >child-denied.txt 2>nul"\r\nexit /b 0\r\n'
            )
            runner = IsolatedCheck(root, script, output)
            result = await runner.execute()
            self.assertEqual(result["exit_code"], 0, result)
            self.assertTrue(result["tree_stopped"])
            self.assertEqual(result["profile_cleanup_hresult"], 0)
            self.assertEqual((root / "work/allowed.txt").read_text().strip(), "GOOD")
            self.assertEqual((root / "work/read.txt").read_text().strip(), "MATERIAL")
            self.assertEqual((root / "work/denied.txt").read_text().strip(), "")
            self.assertEqual((root / "work/child.txt").read_text().strip(), "CHILD")
            self.assertEqual((root / "work/child-denied.txt").read_text().strip(), "")
            self.assertEqual((root / "source/material.txt").read_text(), "MATERIAL")
            self.assertEqual((base / "secret.txt").read_text(), "SYNTHETIC_SECRET")

    async def test_cancel_waits_for_child_job_exit_even_when_cancelled_twice(self):
        from assistant_runtime.platforms.windows_isolation import IsolatedCheck
        from assistant_runtime.api.system import system

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            system.private(root, directory=True)
            for name in ("work", "source", "checks", "runs"):
                (root / name).mkdir()
            output = root / "runs" / uuid.uuid4().hex
            output.mkdir()
            script = root / "checks/loop.cmd"
            script.write_bytes(
                b'@echo off\r\ncmd /d /c "for /L %%i in (1,1,2147483647) do @echo tick>>ticks.txt"\r\n'
            )
            runner = IsolatedCheck(root, script, output)
            task = asyncio.create_task(runner.execute())
            ticks = root / "work/ticks.txt"
            try:
                async with asyncio.timeout(5):
                    while not ticks.exists() or ticks.stat().st_size == 0:
                        if task.done():
                            self.fail(str(task.result()))
                        await asyncio.sleep(0.01)
            finally:
                task.cancel()
                await asyncio.sleep(0)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            self.assertTrue(runner.evidence["tree_stopped"], runner.evidence)
            self.assertEqual(runner.evidence["profile_cleanup_hresult"], 0)
            size = ticks.stat().st_size
            await asyncio.sleep(0.1)
            self.assertEqual(ticks.stat().st_size, size)
