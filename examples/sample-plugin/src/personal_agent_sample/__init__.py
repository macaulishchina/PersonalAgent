"""Standalone trusted extension: no runtime or bundled implementation imports."""

import asyncio
import base64
import hashlib
import json
from pathlib import Path
from assistant_runtime.api import (
    ConfigurationSpec,
    Manifest,
    PluginDefinition,
    Tool,
    ExecutionRequest,
    FileRequest,
)


class Notes:
    def __init__(self, context):
        self.context = context
        self.submissions = asyncio.Lock()

    async def start(self):
        io = self.context.resource("system_io")
        self.context.state_dir.mkdir(parents=True, exist_ok=True)
        io.private(self.context.state_dir, directory=True)
        io.atomic_write(self.context.state_dir / "runtime.json", json.dumps({"platform": io.name}).encode())
        policy = self.context.configuration_resource("policy")
        self.policy = json.loads(policy.read_text()) if policy.exists() else {}
        self.context.log.info("sample ready")

    async def read(self, arguments):
        return {"text": self.context.get("text", "")}

    async def write(self, arguments):
        self.context.set("text", arguments["text"])
        self.context.publish("sample.updated", length=len(arguments["text"]))
        return await self.read({})

    async def report(self, arguments):
        """Submit, never synchronously wait for a nested agent to finish."""
        digest = hashlib.sha256(arguments["text"].encode()).hexdigest()
        key = "report:" + arguments["request_id"]
        async with self.submissions:
            saved = self.context.get(key)
            if saved and saved["digest"] != digest:
                raise ValueError("request_id reused with different report input")
            if not saved:
                resource = await self.context.invoke(
                    FileRequest(
                        "put",
                        {"name": "input.txt", "data": base64.b64encode(arguments["text"].encode()).decode()},
                    )
                )
                saved = {"digest": digest, "input_id": resource["id"]}
                self.context.set(key, saved)
            return await self.context.invoke(
                ExecutionRequest(
                    "submit",
                    {
                        "request_id": arguments["request_id"],
                        "inputs": [saved["input_id"]],
                        "text": "Read the registered input and write a concise report to ../artifacts/report.md. Report the result.",
                        "tools": ["read", "write"],
                        "skills": [],
                    },
                )
            )

    async def report_status(self, arguments):
        return await self.context.invoke(ExecutionRequest("get", {"id": arguments["id"]}))

    def tools(self):
        value = {
            "type": "object",
            "properties": {"text": {"type": "string", "maxLength": 2000}},
            "required": ["text"],
            "additionalProperties": False,
        }
        return (
            Tool(
                "sample_read",
                "Read the sample note",
                {"type": "object", "additionalProperties": False},
                value,
                self.read,
                read_only=True,
                idempotent=True,
            ),
            Tool(
                "sample_write",
                "Replace the sample note",
                {
                    **value,
                    "properties": {
                        "text": {"type": "string", "maxLength": self.context.config["max_length"]}
                    },
                },
                value,
                self.write,
                read_only=False,
                idempotent=True,
            ),
            Tool(
                "sample_report",
                "Submit an input-processing execution; query it separately",
                {
                    "type": "object",
                    "properties": {
                        "request_id": {"type": "string", "format": "uuid"},
                        "text": {"type": "string", "maxLength": 2000},
                    },
                    "required": ["request_id", "text"],
                    "additionalProperties": False,
                },
                {"type": "object"},
                self.report,
                read_only=False,
                idempotent=True,
                external=True,
            ),
            Tool(
                "sample_report_status",
                "Inspect report execution, result and artifacts",
                {
                    "type": "object",
                    "properties": {"id": {"type": "string"}},
                    "required": ["id"],
                    "additionalProperties": False,
                },
                {"type": "object"},
                self.report_status,
                read_only=True,
                idempotent=True,
            ),
        )


def definition():
    from assistant_runtime.api.plugins import plugin_platforms

    return PluginDefinition(
        Manifest("sample", ("execution", "workfiles")),
        Notes,
        None,
        (Path(__file__).parent / "skills" / "sample-notes",),
        uses=(ExecutionRequest, FileRequest),
        supported_platforms=plugin_platforms(Path(__file__).parent),
        resources=("system_io",),
        configuration=ConfigurationSpec(
            **json.loads((Path(__file__).parent / "configuration.json").read_text())
        ),
    )
