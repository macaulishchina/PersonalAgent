"""The same OS contracts run on real Linux and Windows, without paid services."""

import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

from assistant_runtime.api.system import system
from assistant_runtime.platforms.ipc import connect, serve
from platform_support import private_mode, process_alive


class PlatformContracts(unittest.IsolatedAsyncioTestCase):
    async def test_lock_excludes_another_process_and_releases_after_owner_death(self):
        with tempfile.TemporaryDirectory(prefix="锁 中文 ") as folder:
            path = Path(folder) / "owner.lock"
            child = "from assistant_runtime.api.system import system; import sys; f=open(sys.argv[1],'a+b'); system.lock(f,blocking=False)"
            with path.open("a+b") as owner:
                system.lock(owner, blocking=False)
                process = await system.spawn(sys.executable, "-c", child, str(path), stderr=-1)
                await process.communicate()
                self.assertNotEqual(process.returncode, 0)
            process = await system.spawn(sys.executable, "-c", child, str(path), stderr=-1)
            await process.communicate()
            self.assertEqual(process.returncode, 0)

    async def test_private_atomic_replacement_and_no_temporary_residue(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "私有 文件.json"
            system.atomic_write(path, b"before")
            system.atomic_write(path, b"after")
            self.assertEqual(path.read_bytes(), b"after")
            self.assertEqual(private_mode(path), 0o600)
            self.assertEqual(list(path.parent.iterdir()), [path])

    async def test_directory_no_clobber_and_identity_preserved(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            source, destination = base / "source", base / "destination"
            source.mkdir()
            destination.mkdir()
            a, b = system.directory(source), system.directory(destination)
            try:
                fd = a.open_file("中文.txt", os.O_RDWR | os.O_CREAT | os.O_EXCL)
                try:
                    os.write(fd, b"content")
                    os.fsync(fd)
                finally:
                    os.close(fd)
                a.link("中文.txt", b, "report.txt")
                self.assertEqual(a.stat("中文.txt").st_ino, b.stat("report.txt").st_ino)
                with self.assertRaises(FileExistsError):
                    a.link("中文.txt", b, "report.txt")
                with self.assertRaises(ValueError):
                    b.open_file("../outside", os.O_RDONLY)
                a.unlink("中文.txt")
                self.assertEqual((destination / "report.txt").read_bytes(), b"content")
            finally:
                a.close()
                b.close()

    async def test_control_protocol_and_disconnect(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "control.sock"
            disconnected = asyncio.Event()

            async def handler(reader, writer):
                try:
                    value = json.loads(await reader.readline())
                    writer.write(json.dumps({"ok": True, "result": value}).encode() + b"\n")
                    await writer.drain()
                    self.assertEqual(await reader.read(1), b"")
                    disconnected.set()
                finally:
                    writer.close()
                    await writer.wait_closed()

            server = await serve(path, handler)
            try:
                reader, writer = await connect(path)
                writer.write(b'{"command":"status"}\n')
                await writer.drain()
                self.assertEqual(json.loads(await reader.readline())["result"], {"command": "status"})
                writer.close()
                await writer.wait_closed()
                await asyncio.wait_for(disconnected.wait(), 3)
            finally:
                server.close()
                await server.wait_closed()

    async def test_control_concurrent_clients_and_message_limit(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "control.sock"

            async def handler(reader, writer):
                try:
                    writer.write(await reader.readline())
                    await writer.drain()
                finally:
                    writer.close()
                    await writer.wait_closed()

            server = await serve(path, handler, limit=1024)

            async def request(number):
                reader, writer = await connect(path, limit=1024)
                try:
                    data = (str(number) + "\n").encode()
                    writer.write(data)
                    await writer.drain()
                    self.assertEqual(await reader.readline(), data)
                finally:
                    writer.close()
                    await writer.wait_closed()

            try:
                await asyncio.wait_for(asyncio.gather(*(request(i) for i in range(12))), 10)
            finally:
                server.close()
                await server.wait_closed()

    async def test_process_cancellation_leaves_unrelated_process_alive(self):
        unrelated = await system.spawn(sys.executable, "-c", "import time;time.sleep(60)")
        process = await system.spawn(sys.executable, "-c", "import time;time.sleep(60)")
        try:
            await system.stop_process(process, timeout=1)
            self.assertFalse(process_alive(process.pid))
            self.assertTrue(process_alive(unrelated.pid))
        finally:
            await system.stop_process(unrelated, timeout=1)

    async def test_hardware_platform_declarations_and_no_optional_imports(self):
        from assistant_runtime.core.plugin_catalog import declaration

        for name in ("pipewire", "v4l2", "hid_buttons"):
            self.assertEqual(declaration(name).supported_platforms, ("linux",))
        self.assertEqual(declaration("search").supported_platforms, ("linux", "windows"))
        from assistant_runtime.core.configuration.catalog import catalog

        rows = {row["id"]: row for row in catalog()["plugins"]}
        self.assertEqual(rows["hid_buttons"]["supported_platforms"], ["linux"])
        self.assertEqual(rows["hid_buttons"]["platform_supported"], sys.platform == "linux")

    async def test_platform_specific_imports_stay_out_of_business(self):
        import ast

        root = Path(__file__).resolve().parents[1] / "src/assistant_runtime"
        forbidden = {"fcntl", "termios", "msvcrt", "win32api", "win32file", "win32job", "win32service"}
        for path in root.rglob("*.py"):
            if "platforms" in path.parts or any(p in path.parts for p in ("hid_buttons", "pipewire", "v4l2")):
                continue
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Import):
                    self.assertFalse({n.name.split(".")[0] for n in node.names} & forbidden, str(path))
                elif isinstance(node, ast.ImportFrom):
                    self.assertNotIn((node.module or "").split(".")[0], forbidden, str(path))


@unittest.skipUnless(sys.platform == "win32", "Windows Job Object, ACL and reparse contracts")
class WindowsContracts(unittest.IsolatedAsyncioTestCase):
    async def test_owner_death_closes_child_and_grandchild_job(self):
        with tempfile.TemporaryDirectory(prefix="job 中文 ") as folder:
            pidfile = Path(folder) / "child.json"
            grandchild = "import time; time.sleep(60)"
            child = (
                "import subprocess,sys,os,json,time; from pathlib import Path; "
                f"p=subprocess.Popen([sys.executable,'-c',{grandchild!r}]); "
                f"Path({str(pidfile)!r}).write_text(json.dumps([os.getpid(),p.pid])); time.sleep(60)"
            )
            owner = (
                "import asyncio,sys; from assistant_runtime.api.system import system\n"
                "async def run():\n"
                f" p=await system.spawn(sys.executable,'-c',{child!r})\n"
                " await asyncio.Event().wait()\n"
                "asyncio.run(run())\n"
            )
            process = await asyncio.create_subprocess_exec(sys.executable, "-c", owner)
            try:
                async with asyncio.timeout(15):
                    while not pidfile.exists():
                        self.assertIsNone(process.returncode)
                        await asyncio.sleep(0.02)
                ids = json.loads(pidfile.read_text())
                process.kill()  # No graceful shutdown hook may run.
                await process.wait()
                async with asyncio.timeout(10):
                    while any(process_alive(pid) for pid in ids):
                        await asyncio.sleep(0.02)
            finally:
                if process.returncode is None:
                    process.kill()
                    await process.wait()

    async def test_busy_replacement_preserves_original_and_cleans_temporary_file(self):
        import win32con
        import win32file

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "secret"
            system.atomic_write(path, b"original")
            handle = win32file.CreateFile(
                str(path),
                win32con.GENERIC_READ,
                win32con.FILE_SHARE_READ,
                None,
                win32con.OPEN_EXISTING,
                0,
                None,
            )
            try:
                with self.assertRaises(OSError):
                    system.atomic_write(path, b"replacement")
                self.assertEqual(path.read_bytes(), b"original")
                self.assertEqual(list(path.parent.iterdir()), [path])
            finally:
                handle.Close()
            system.atomic_write(path, b"replacement")
            self.assertEqual(private_mode(path), 0o600)

    async def test_junction_and_reserved_names_are_rejected(self):
        import subprocess

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "outside").mkdir()
            junction = root / "junction"
            subprocess.run(
                [os.environ["COMSPEC"], "/d", "/c", "mklink", "/J", str(junction), str(root / "outside")],
                check=True,
                capture_output=True,
            )
            try:
                with self.assertRaises(ValueError):
                    system.directory(junction)
            finally:
                junction.rmdir()
            for name in ("CON", "con.txt", "LPT1", "a:stream", "trailing.", "trailing "):
                with self.subTest(name=name), self.assertRaises(ValueError):
                    system.component(name)
