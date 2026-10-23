"""Async Mem0 provider. The worker owns indexes; inference uses the models capability."""

import asyncio
import json
import os
from pathlib import Path
import signal
import sys


class Mem0Memory:
    def __init__(self, root, cache, complete=None, project=""):
        self.root, self.cache = Path(root), Path(cache)
        self.complete, self.project = complete, project
        self.process = None
        self.lock = asyncio.Lock()
        self.start_lock = asyncio.Lock()

    async def start(self):
        async with self.start_lock:
            if self.process and self.process.returncode is None:
                return
            self.root.mkdir(parents=True, exist_ok=True)
            env = {
                k: v
                for k, v in os.environ.items()
                if not k.startswith(("OPENAI_", "ANTHROPIC_", "CODEX_", "MEM0_"))
            }
            log = (self.root / "worker.log").open("a")
            try:
                self.process = await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-m",
                    "assistant_runtime.memory.mem0_worker",
                    str(self.root),
                    str(self.cache),
                    env=env,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=log,
                    start_new_session=True,
                    limit=4 * 1024 * 1024,
                )
            finally:
                log.close()
            try:
                line = await asyncio.wait_for(self.process.stdout.readline(), 90)
                if not line or not json.loads(line).get("ready"):
                    raise RuntimeError("Mem0 startup failed; inspect its local worker log")
            except BaseException:
                await self.close()
                raise

    async def call(self, action, **arguments):
        async with self.lock:
            await self.start()
            try:
                await self.send({"action": action, "arguments": arguments})
                async with asyncio.timeout(300 if action == "capture" else 60):
                    while True:
                        line = await self.process.stdout.readline()
                        if not line:
                            raise RuntimeError("Mem0 worker disconnected; write outcome may be unknown")
                        reply = json.loads(line)
                        if "model_request" in reply:
                            if self.complete is None:
                                raise RuntimeError("Memory model callback not configured")
                            request = reply["model_request"]
                            response = await self.complete(request["messages"], request["schema"])
                            await self.send({"model_result": response})
                            continue
                        if "error" in reply:
                            raise RuntimeError(reply["error"])
                        return reply["result"]
            except BaseException:
                # Kill the owned memory worker before releasing the operation lock.
                # No canceled thread can keep writing after a correction or forget.
                await self.close()
                raise

    async def send(self, payload):
        data = (json.dumps(payload, ensure_ascii=False) + "\n").encode()
        if len(data) > 4 * 1024 * 1024:
            raise ValueError("Memory request exceeds size limit")
        self.process.stdin.write(data)
        await self.process.stdin.drain()

    async def export(self):
        return await self.call("export")

    async def read(self, ident):
        return await self.call("read", id=ident)

    async def upsert(self, record):
        await self.call("upsert", record=record)

    async def delete(self, ident):
        await self.call("delete", id=ident)

    async def search(self, query, scope="user"):
        return await self.call("search", query=query, scope=scope, project=self.project)

    async def capture(self, sources, scope, project, job_id):
        return await self.call("capture", sources=sources, scope=scope, project=project, job_id=job_id)

    async def import_records(self, records):
        for record in records:
            await self.upsert(record)

    async def close(self):
        process, self.process = self.process, None
        if process and process.returncode is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
                await asyncio.wait_for(process.wait(), 5)
            except TimeoutError:
                os.killpg(process.pid, signal.SIGKILL)
                await process.wait()
            except ProcessLookupError:
                await process.wait()
