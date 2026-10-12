"""Basic Memory adapter: upstream owns Markdown, indexing and hybrid retrieval."""

import asyncio
import json
import os
from pathlib import Path
import re
import signal
import sys


class BasicMemory:
    def __init__(self, root, python=None):
        self.root = Path(root)
        self.python = python or sys.executable
        self.process = None
        self.lock = asyncio.Lock()
        self.start_lock = asyncio.Lock()

    async def start(self):
        async with self.start_lock:
            await self._start()

    async def _start(self):
        if self.process and self.process.returncode is None:
            return
        self.root.mkdir(parents=True, exist_ok=True)
        log = (self.root / "mcp.log").open("a")
        try:
            self.process = await asyncio.create_subprocess_exec(
                self.python,
                str(Path(__file__).with_name("worker.py")),
                str(self.root),
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
                raise RuntimeError("Basic Memory failed to initialize; inspect memory/mcp.log")
        except BaseException:
            await self.close()
            raise

    async def call(self, tool, **arguments):
        async with self.lock:
            await self.start()
            try:
                self.process.stdin.write(
                    (
                        json.dumps(
                            {
                                "tool": tool,
                                "arguments": {"project": "main", "output_format": "json", **arguments},
                            }
                        )
                        + "\n"
                    ).encode()
                )
                await self.process.stdin.drain()
                line = await asyncio.wait_for(self.process.stdout.readline(), 60)
                response = json.loads(line)
            except BaseException:
                # Never let a timed-out response be consumed as the next request's result.
                await self.close()
                raise
            if "error" in response:
                raise RuntimeError(response["error"])
            result = response["result"]
            if result.get("structuredContent"):
                structured = result["structuredContent"]
                return structured["result"] if set(structured) == {"result"} else structured
            content = "\n".join(c.get("text", "") for c in result.get("content", []))
            try:
                return json.loads(content)
            except ValueError:
                return {"text": content}

    @staticmethod
    def validate_id(ident):
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", ident):
            raise ValueError("Invalid logical memory ID")
        return ident

    async def upsert(self, record):
        ident = self.validate_id(record["id"])
        await self.call(
            "write_note",
            title=ident,
            directory="memories",
            overwrite=True,
            content=f"# {record['title']}\n\n{record['text']}\n",
            metadata={
                "logical_id": ident,
                "scope": record["scope"],
                "agent_record": json.dumps(record, ensure_ascii=False),
            },
        )

    def _records(self):
        import yaml

        records = []
        for path in (self.root / "notes/memories").glob("*.md"):
            parts = path.read_text().split("---", 2)
            if len(parts) == 3:
                metadata = yaml.safe_load(parts[1]) or {}
                if "agent_record" in metadata:
                    records.append(json.loads(metadata["agent_record"]))
        return records

    async def export(self):
        return await asyncio.to_thread(self._records)

    async def read(self, ident):
        self.validate_id(ident)
        return next((r for r in await self.export() if r["id"] == ident), None)

    async def search(self, query, scope="user"):
        result = await self.call(
            "search_notes",
            query=query,
            search_type="hybrid",
            page_size=12,
            min_similarity=0.25,
        )
        # Resolve upstream hit identifiers to our portable records. No custom ranking/index.
        records = {r["id"]: r for r in await self.export() if r["scope"] == scope}
        ordered = []
        for hit in result.get("results", []):
            ident = hit.get("title") or Path(hit.get("file_path", "")).stem
            if ident in records and records[ident] not in ordered:
                ordered.append(records[ident])
        return ordered[:8]

    async def delete(self, ident):
        self.validate_id(ident)
        if await self.read(ident):
            await self.call("delete_note", identifier=f"memories/{ident}")

    async def import_records(self, records):
        for record in records:
            await self.upsert(record)

    async def close(self):
        p, self.process = self.process, None
        if p:
            try:
                os.killpg(p.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(p.wait(), 5)
            except TimeoutError:
                try:
                    os.killpg(p.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await p.wait()
