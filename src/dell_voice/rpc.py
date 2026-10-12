"""An isolated stdio connection to Codex, with no changes to the desktop daemon."""
import asyncio
import json
import logging
import os

log = logging.getLogger(__name__)


class RpcError(RuntimeError):
    pass


class CodexClient:
    def __init__(self, binary="codex", cwd=None):
        self.binary, self.cwd = binary, cwd
        self.process = None
        self.events = asyncio.Queue()
        self.pending = {}
        self.counter = 0
        self.readers = []

    async def start(self):
        env = os.environ.copy()
        # The Live project key must never switch Codex away from its existing login.
        env.pop("OPENAI_API_KEY", None)
        self.process = await asyncio.create_subprocess_exec(
            self.binary, "app-server", "--listen", "stdio://",
            cwd=self.cwd, env=env,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, limit=16 * 1024 * 1024,
        )
        self.readers = [asyncio.create_task(self._read()), asyncio.create_task(self._stderr())]
        await self.request("initialize", {
            "clientInfo": {"name": "dell_voice_assistant", "title": "DELL Voice Assistant", "version": "0.1.0"},
            "capabilities": {"experimentalApi": True},
        })
        await self.send({"method": "initialized"})

    async def send(self, message):
        if not self.process or self.process.returncode is not None:
            raise RpcError("Codex backend is not running")
        self.process.stdin.write((json.dumps(message, ensure_ascii=False) + "\n").encode())
        await self.process.stdin.drain()

    async def request(self, method, params, timeout=30):
        self.counter += 1
        ident = self.counter
        future = asyncio.get_running_loop().create_future()
        self.pending[ident] = future
        try:
            await self.send({"id": ident, "method": method, "params": params})
            return await asyncio.wait_for(future, timeout)
        finally:
            self.pending.pop(ident, None)

    async def respond(self, ident, result):
        await self.send({"id": ident, "result": result})

    async def _read(self):
        try:
            while line := await self.process.stdout.readline():
                msg = json.loads(line)
                if "method" not in msg and msg.get("id") in self.pending:
                    f = self.pending[msg["id"]]
                    if not f.done():
                        if "error" in msg:
                            f.set_exception(RpcError(str(msg["error"])))
                        else:
                            f.set_result(msg.get("result", {}))
                else:
                    await self.events.put(msg)
        except (ValueError, OSError) as exc:
            log.warning("Codex event stream failed: %s", type(exc).__name__)
        finally:
            for future in list(self.pending.values()):
                if not future.done():
                    future.set_exception(RpcError("Codex connection closed; request outcome may be unknown"))
            await self.events.put({"method": "local/disconnected", "params": {}})

    async def _stderr(self):
        # Do not copy backend diagnostics that might contain account metadata into our logs.
        while await self.process.stderr.readline():
            pass

    async def close(self):
        if self.process and self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), 5)
            except TimeoutError:
                self.process.kill()
                await self.process.wait()
        for task in self.readers:
            task.cancel()
        await asyncio.gather(*self.readers, return_exceptions=True)

