"""ACL-bound byte pipes with cancellable overlapped I/O; no TCP or pickle."""

import asyncio
import hashlib
from pathlib import Path
import logging


def pipe_name(path):
    identity = str(Path(path).absolute()).casefold().encode("utf-8")
    return "\\\\.\\pipe\\personal-agent-" + hashlib.sha256(identity).hexdigest()


async def operation(handle, start, *, eof=False):
    import pywintypes
    import win32event
    import win32file

    overlapped = pywintypes.OVERLAPPED()
    overlapped.hEvent = win32event.CreateEvent(None, True, False, None)

    try:
        buffer = start(overlapped)
        if buffer is True:  # ConnectNamedPipe reported an already-connected client.
            overlapped.hEvent.Close()
            return None
    except pywintypes.error as exc:
        overlapped.hEvent.Close()
        if eof and exc.winerror in {109, 232, 233, 995}:
            return b""
        raise OSError(exc.winerror, "Local pipe operation failed") from exc
    except BaseException:
        overlapped.hEvent.Close()
        raise

    def work():
        try:
            size = win32file.GetOverlappedResult(handle, overlapped, True)
            return bytes(buffer[:size]) if buffer is not None else None
        except pywintypes.error as exc:
            if eof and exc.winerror in {109, 232, 233, 995}:  # closed/cancelled peer
                return b""
            raise OSError(exc.winerror, "Local pipe operation failed") from exc

    task = asyncio.create_task(asyncio.to_thread(work))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            win32file.CancelIoEx(handle, overlapped)
        except pywintypes.error:
            pass
        await asyncio.gather(task, return_exceptions=True)
        raise
    finally:
        overlapped.hEvent.Close()


class Stream:
    def __init__(self, handle, limit):
        self.handle, self.limit = handle, limit
        self.input, self.output = bytearray(), bytearray()
        self.closed = False

    async def read(self, n=-1):
        if self.input:
            count = len(self.input) if n < 0 else min(n, len(self.input))
            result = bytes(self.input[:count])
            del self.input[:count]
            return result
        if self.closed:
            return b""
        import win32file

        size = min(n if n > 0 else 65536, 65536)
        return await operation(self.handle, lambda ov: win32file.ReadFile(self.handle, size, ov)[1], eof=True)

    async def readline(self):
        pending = bytearray()
        while True:
            chunk = await self.read(65536)
            if not chunk:
                return bytes(pending)
            pending.extend(chunk)
            split = pending.find(b"\n")
            if split >= 0:
                if split + 1 > self.limit:
                    raise ValueError("Control message exceeds limit")
                self.input.extend(pending[split + 1 :])
                return bytes(pending[: split + 1])
            if len(pending) > self.limit:
                raise ValueError("Control message exceeds limit")

    def write(self, data):
        if self.closed:
            raise ConnectionError("Control pipe closed")
        if len(self.output) + len(data) > self.limit:
            raise ValueError("Control message exceeds limit")
        self.output.extend(data)

    def is_closing(self):
        return self.closed

    async def drain(self):
        import win32file

        data = bytes(self.output)
        self.output.clear()
        if data:
            await operation(self.handle, lambda ov: (win32file.WriteFile(self.handle, data, ov), None)[1])

    def close(self):
        if not self.closed:
            self.closed = True
            import win32file

            import pywintypes

            try:
                win32file.CancelIoEx(self.handle, None)
            except pywintypes.error as exc:
                if exc.winerror != 1168:  # no pending operation
                    raise
            finally:
                self.handle.Close()

    async def wait_closed(self):
        return


async def connect(path, *, limit):
    import pywintypes
    import win32con
    import win32file

    # Only retry connection establishment; no request has been sent. The server
    # creates its next listener after accepting the previous client.
    deadline = asyncio.get_running_loop().time() + 2
    while True:
        try:
            handle = win32file.CreateFile(
                pipe_name(path),
                win32con.GENERIC_READ | win32con.GENERIC_WRITE,
                0,
                None,
                win32con.OPEN_EXISTING,
                win32con.FILE_FLAG_OVERLAPPED,
                None,
            )
            break
        except pywintypes.error as exc:
            if exc.winerror != 231 or asyncio.get_running_loop().time() >= deadline:
                raise OSError(exc.winerror, "Local control pipe unavailable") from exc
            await asyncio.sleep(0.01)
    try:
        import win32pipe
        from .windows import verify_process_owner

        verify_process_owner(win32pipe.GetNamedPipeServerProcessId(handle))
    except BaseException:
        handle.Close()
        raise
    stream = Stream(handle, limit)
    return stream, stream


class Server:
    def __init__(self, path, callback, limit):
        self.path, self.callback, self.limit = path, callback, limit
        self.clients = set()
        self.ready = asyncio.get_running_loop().create_future()
        self.task = asyncio.create_task(self.accept())
        self.task.add_done_callback(self.completed)

    def completed(self, task):
        self.clients.discard(task)
        if not task.cancelled() and task.exception() is not None:
            logging.getLogger(__name__).error("Local control handler failed", exc_info=task.exception())

    async def accept(self):
        import pywintypes
        import win32con
        import win32pipe
        from .windows import security_attributes

        while True:
            handle = win32pipe.CreateNamedPipe(
                pipe_name(self.path),
                win32pipe.PIPE_ACCESS_DUPLEX | win32con.FILE_FLAG_OVERLAPPED,
                win32pipe.PIPE_TYPE_BYTE | win32pipe.PIPE_WAIT | 0x8,  # reject remote clients
                64,
                65536,
                65536,
                0,
                security_attributes(),
            )
            stream = Stream(handle, self.limit)
            if not self.ready.done():
                self.ready.set_result(None)
            try:

                def start(ov):
                    try:
                        win32pipe.ConnectNamedPipe(handle, ov)
                    except pywintypes.error as exc:
                        if exc.winerror == 535:  # client connected before ConnectNamedPipe
                            return True
                        elif exc.winerror != 997:
                            raise
                    return None

                await operation(handle, start)
            except BaseException:
                stream.close()
                raise
            if len(self.clients) >= 63:
                stream.close()
                continue

            async def client(connection):
                try:
                    from .windows import verify_process_owner

                    verify_process_owner(win32pipe.GetNamedPipeClientProcessId(connection.handle))
                    await self.callback(connection, connection)
                finally:
                    connection.close()

            task = asyncio.create_task(client(stream))
            self.clients.add(task)
            task.add_done_callback(self.completed)

    def close(self):
        self.task.cancel()
        for client in self.clients:
            client.cancel()

    async def wait_closed(self):
        await asyncio.gather(self.task, *self.clients, return_exceptions=True)


async def serve(path, callback, *, limit):
    server = Server(path, callback, limit)
    try:
        done, _ = await asyncio.wait((server.ready, server.task), return_when=asyncio.FIRST_COMPLETED)
        if server.task in done:
            await server.task
    except BaseException:
        server.close()
        await server.wait_closed()
        raise
    return server
