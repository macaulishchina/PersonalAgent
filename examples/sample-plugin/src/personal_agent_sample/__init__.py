"""Standalone trusted extension: no runtime or bundled implementation imports."""

import asyncio
import base64
import hashlib
import json
from pathlib import Path
import uuid
from assistant_runtime.api import (
    ConfigurationSpec,
    plugin_manifest,
    PluginDefinition,
    Tool,
    ExecutionRequest,
    FileRequest,
)
from assistant_runtime.api.tool_calls import invalid_arguments


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
        self.policy = json.loads(policy.read_text(encoding="utf-8")) if policy.exists() else {}
        self.context.log.info("sample ready")

    async def read(self, arguments):
        return {"text": self.context.get("text", "")}

    async def write(self, arguments):
        if len(arguments["text"]) > self.context.config["max_length"]:
            raise invalid_arguments("Note exceeds the configured max_length; shorten text", "text")
        if self.context.get("text", "") == arguments["text"]:
            return await self.read({})
        self.context.set("text", arguments["text"])
        self.context.publish("sample.updated", length=len(arguments["text"]))
        return await self.read({})

    async def prepare_configuration(self, settings):
        from assistant_runtime.api.configuration import ConfigurationUpdate

        path = self.context.configuration_resource("policy")
        policy = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        previous = self.policy

        async def commit():
            self.policy = policy

        async def rollback():
            self.policy = previous

        return ConfigurationUpdate(commit, rollback)

    async def report(self, arguments):
        """Submit, never synchronously wait for a nested agent to finish."""
        try:
            if str(uuid.UUID(arguments["request_id"])) != arguments["request_id"]:
                raise ValueError()
        except (ValueError, TypeError, AttributeError):
            raise invalid_arguments("Use a canonical UUID request_id", "request_id") from None
        digest = hashlib.sha256(arguments["text"].encode()).hexdigest()
        key = "report:" + arguments["request_id"]
        async with self.submissions:
            saved = self.context.get(key)
            if saved and saved["digest"] != digest:
                raise invalid_arguments(
                    "request_id already identifies different report input; inspect that execution",
                    "request_id",
                )
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
                "Replace the sample note; text must also fit the configured max_length",
                {
                    **value,
                    "properties": {"text": {"type": "string", "maxLength": 2000}},
                    "examples": [{"text": "A short note"}, {"text": ""}],
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
                    "examples": [
                        {"request_id": "b1d5a4fb-77ed-4b29-8b45-7571d98bb912", "text": "Summarize this input"}
                    ],
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
        plugin_manifest(__file__),
        Notes,
        None,
        (Path(__file__).parent / "skills" / "sample-notes",),
        uses=(ExecutionRequest, FileRequest),
        supported_platforms=plugin_platforms(Path(__file__).parent),
        resources=("system_io",),
        configuration=ConfigurationSpec(
            **json.loads((Path(__file__).parent / "configuration.json").read_text(encoding="utf-8"))
        ),
    )
