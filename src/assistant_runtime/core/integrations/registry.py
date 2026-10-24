"""Versioned MCP definitions; runtime state and credentials stay outside Git."""

from assistant_runtime.api.system import system

import asyncio
import copy
import json
import os
from pathlib import Path
import tempfile

from assistant_runtime.core.integrations.settings import IntegrationSettings, definition, name


class McpRegistry:
    def __init__(self, root, settings):
        self.root = Path(root).resolve()
        self.settings = IntegrationSettings.parse(settings)
        root = Path(root)
        self.path = (
            Path(self.settings.registry_path)
            if self.settings.registry_path
            else root / "config/mcp/servers.json"
        )
        self.work = root / "state/mcp/servers"
        self.lock = asyncio.Lock()
        self.data = {"revision": 0, "servers": {}, "removed": []}
        self.reload()

    @property
    def revision(self):
        return self.data["revision"]

    def reload(self):
        if not self.path.exists():
            if self.revision:
                raise ValueError("MCP registry disappeared; retaining current configuration")
            return
        if self.path.stat().st_size > 512 * 1024:
            raise ValueError("MCP registry exceeds 512 KiB")
        data = json.loads(self.path.read_text(encoding="utf-8"))
        from .configuration import validate_registry

        data = validate_registry(data, self.settings.max_servers)
        if data != self.data and data["revision"] <= self.revision:
            raise ValueError("Increment registry revision when editing MCP configuration")
        self.data = data

    def snapshot(self):
        return copy.deepcopy(self.data)

    def get(self, ident):
        name(ident)
        if ident not in self.data["servers"]:
            raise ValueError("MCP server not found")
        return copy.deepcopy(self.data["servers"][ident])

    def save(self, data):
        raw = json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False).encode()
        if len(raw) > 512 * 1024:
            raise ValueError("MCP registry exceeds 512 KiB")
        from assistant_runtime.api.configuration_io import configuration_lock

        with configuration_lock(self.root):
            if (self.root / "state/configuration/transaction.json").exists():
                raise ValueError("Configuration recovery is required before writing")
            current = (
                json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {"revision": 0}
            )
            if current["revision"] != self.revision:
                raise ValueError("MCP registry changed; reload before writing")
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with tempfile.NamedTemporaryFile(dir=self.path.parent, delete=False) as handle:
                temporary = Path(handle.name)
                try:
                    system.private(temporary)
                    handle.write(raw)
                    handle.flush()
                    os.fsync(handle.fileno())
                    handle.close()  # Windows cannot rename an open temporary file.
                    os.replace(temporary, self.path)
                finally:
                    handle.close()
                    temporary.unlink(missing_ok=True)
            self.data = data

    def configure(self, ident, changes, revision):
        name(ident)
        if not isinstance(changes, dict):
            raise ValueError("MCP changes must be an object")
        data = self.snapshot()
        previous = data["servers"].get(ident)
        spec = definition({**(previous["definition"] if previous else {}), **changes})
        if previous and previous["definition"] == spec:
            return self.get(ident)
        if type(revision) is not int or revision != self.revision:
            raise ValueError("MCP registry changed; read its latest revision")
        if not previous and len(data["servers"]) >= self.settings.max_servers:
            raise ValueError("MCP server capacity reached")
        data["revision"] += 1
        data["servers"][ident] = {"revision": data["revision"], "definition": spec}
        data["removed"] = [item for item in data["removed"] if item != ident]
        self.save(data)
        return self.get(ident)

    def remove(self, ident, revision):
        self.get(ident)
        if type(revision) is not int or revision != self.revision:
            raise ValueError("MCP registry changed; read its latest revision")
        data = self.snapshot()
        if len(data["removed"]) >= 256:
            raise ValueError("MCP removal history capacity reached")
        del data["servers"][ident]
        data["removed"].append(ident)
        data["revision"] += 1
        self.save(data)

    def public(self, ident):
        row = self.get(ident)
        spec = row["definition"]
        t = spec["transport"]
        if t["type"] == "stdio":
            transport = {
                "type": "stdio",
                "command": t["command"],
                "argument_count": len(t["args"]),
                "cwd": t.get("cwd") or str(self.work / ident),
                "env_names": sorted(t["env"]),
                "env_vars": t["env_vars"],
            }
        else:
            from urllib.parse import urlsplit

            parsed = urlsplit(t["url"])
            transport = {
                "type": "http",
                "origin": parsed.scheme + "://" + parsed.netloc,
                "header_names": sorted(t["headers"]),
                "env_headers": t["env_headers"],
                "bearer_token_env": t.get("bearer_token_env"),
            }
        return {
            "name": ident,
            "runtime_name": "external_" + ident,
            **row,
            "definition": {**spec, "transport": transport},
        }
