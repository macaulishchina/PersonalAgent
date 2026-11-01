"""Shared implementations. OS-specific operations are confined to this package."""

import asyncio
import errno
import os
from pathlib import Path
import signal
import tempfile
import sys


class Platform:
    def directory(self, path):
        from .directory import Directory

        return Directory(path)

    def component(self, name):
        from .directory import component

        return component(name)

    def supervised_command(self, timeout, args):
        if sys.platform == "win32":
            return args
        return [sys.executable, "-m", "assistant_runtime.platforms.linux_guardian", str(timeout), *args]

    @property
    def name(self):
        return "windows" if sys.platform == "win32" else sys.platform

    def lock(self, stream, *, blocking=True):
        import portalocker

        flags = portalocker.LOCK_EX | (0 if blocking else portalocker.LOCK_NB)
        try:
            portalocker.lock(stream, flags)
        except portalocker.exceptions.LockException as exc:
            raise BlockingIOError(errno.EAGAIN, "File is owned by another process") from exc

    def private(self, path, *, directory=False):
        if sys.platform == "win32":
            from .windows import private

            private(path, directory=directory)
        else:
            os.chmod(path, 0o700 if directory else 0o600)

    def sync_directory(self, path):
        if sys.platform == "win32":
            # Atomic replacement uses MOVEFILE_WRITE_THROUGH on Windows.
            # FlushFileBuffers on directory handles is not a portable equivalent.
            return
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def atomic_write(self, path, data, *, prefix=".private-"):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if sys.platform == "win32":
            self.private(path.parent, directory=True)
        fd, name = tempfile.mkstemp(prefix=prefix, dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                self.private(name)
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            if sys.platform == "win32":
                import win32file
                import pywintypes
                from .windows import file_error

                import time

                # Readers/scanners can briefly omit FILE_SHARE_DELETE. Retry only
                # the atomic replacement, never the work that produced the data.
                for attempt in range(6):
                    try:
                        win32file.MoveFileEx(name, str(path), 0x1 | 0x8)  # REPLACE_EXISTING | WRITE_THROUGH
                        break
                    except pywintypes.error as exc:
                        if exc.winerror not in {5, 32, 33} or attempt == 5:
                            raise file_error(exc, path) from exc
                        time.sleep(0.01 * (2**attempt))
            else:
                os.replace(name, path)
                self.sync_directory(path.parent)
        finally:
            Path(name).unlink(missing_ok=True)

    async def spawn(self, *args, **kwargs):
        if sys.platform == "win32":
            from .windows_process import spawn

            return await spawn(*args, **kwargs)
        kwargs.setdefault("start_new_session", True)
        return await asyncio.create_subprocess_exec(*args, **kwargs)

    def signal_process(self, process, *, force=False):
        if sys.platform == "win32":
            process.kill() if force else process.terminate()
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL if force else signal.SIGTERM)
            except ProcessLookupError:
                pass

    async def stop_process(self, process, *, timeout=5, graceful=False):
        if process is None:
            return
        if process.returncode is None:
            if not graceful:
                self.signal_process(process)
            try:
                await asyncio.wait_for(process.wait(), timeout)
            except TimeoutError:
                self.signal_process(process, force=True)
                await process.wait()
        if hasattr(process, "close_job"):
            process.close_job()

    def stop_signals(self, callback):
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            if sys.platform == "win32":
                signal.signal(sig, lambda *_: loop.call_soon_threadsafe(callback))
            else:
                loop.add_signal_handler(sig, callback)

    async def terminal_input(self, *, secret):
        if sys.platform == "win32":
            from .windows import terminal_input
        else:
            from .posix import terminal_input
        return await terminal_input(secret=secret)
