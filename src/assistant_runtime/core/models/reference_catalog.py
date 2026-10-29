"""Instance-local models.dev metadata; reads stay available during bounded refreshes."""

import asyncio
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

import aiohttp
from assistant_runtime.api.system import system
from .capabilities import public_catalog, facts

URL = "https://models.dev/api.json"
MAX_BYTES = 16 * 1024 * 1024


def compile_catalog(raw):
    models = {}
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("Invalid model catalog")
    for provider, spec in payload.items():
        if not isinstance(spec, dict) or not isinstance(spec.get("models", {}), dict):
            raise ValueError("Invalid provider catalog")
        for ident, row in spec.get("models", {}).items():
            if not isinstance(row, dict) or len(provider + "/" + ident) > 512:
                raise ValueError("Invalid model record")
            value = {}
            for source, target in (
                ("tool_call", "tools"),
                ("reasoning", "reasoning"),
                ("structured_output", "structured"),
            ):
                if type(row.get(source)) is bool:
                    value[target] = row[source]
            inputs = row.get("modalities", {}).get("input")
            if isinstance(inputs, list):
                value["images"] = "image" in inputs
            for source, target in (("context", "context_window"), ("output", "output_limit")):
                n = row.get("limit", {}).get(source)
                if type(n) is int and 1 <= n <= 10000000:
                    value[target] = n
            # Read explicit effort choices only. Toggle and token budgets are different controls.
            efforts = []
            for option in (
                row.get("reasoning_options", []) if isinstance(row.get("reasoning_options"), list) else []
            ):
                if (
                    not isinstance(option, dict)
                    or option.get("type") != "effort"
                    or not isinstance(option.get("values"), list)
                ):
                    continue
                for effort in option["values"]:
                    if (
                        effort in ("none", "minimal", "low", "medium", "high", "xhigh", "max")
                        and effort not in efforts
                    ):
                        efforts.append(effort)
            if efforts and row.get("reasoning") is True:
                value["reasoning_efforts"] = efforts
            canonical = row.get("canonical_model_id")
            if (
                isinstance(canonical, str)
                and len(canonical) <= 512
                and not any(c.isspace() or ord(c) < 32 for c in canonical)
                and canonical.partition("/")[0]
                and canonical.partition("/")[2]
            ):
                value["canonical_model_id"] = canonical
            if value:
                models[provider + "/" + ident] = value
    if not models:
        raise ValueError("Empty model catalog")
    return {
        "source": URL,
        "revision": hashlib.sha256(raw).hexdigest(),
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "models": models,
    }


def validate_cache(data):
    if (
        not isinstance(data, dict)
        or data.get("source") != URL
        or not isinstance(data.get("revision"), str)
        or len(data["revision"]) != 64
        or any(c not in "0123456789abcdef" for c in data["revision"])
        or not isinstance(data.get("models"), dict)
        or not 1 <= len(data["models"]) <= 100000
    ):
        raise ValueError("Invalid catalog cache")
    stamp = datetime.fromisoformat(data["retrieved_at"])
    if stamp.tzinfo is None:
        raise ValueError("Catalog timestamp requires timezone")
    for name, row in data["models"].items():
        if (
            not isinstance(name, str)
            or not 1 <= len(name) <= 512
            or "/" not in name
            or any(ord(c) < 32 for c in name)
            or not isinstance(row, dict)
            or {k: v for k, v in row.items() if k != "canonical_model_id"} != facts(row)
        ):
            raise ValueError("Invalid cached model")
    return data


class ReferenceCatalog:
    def __init__(self, root):
        self.path = Path(root) / "state/model-catalog/models.dev.json"
        self.data = public_catalog()
        self.error = None
        self.checked_at = None
        self.task = None
        self.closed = False
        if self.path.is_file():
            try:
                if self.path.stat().st_size > MAX_BYTES:
                    raise ValueError("Catalog cache too large")
                data = json.loads(self.path.read_text(encoding="utf-8"))
                validate_cache(data)
                if datetime.fromisoformat(data["retrieved_at"]) >= datetime.fromisoformat(
                    self.data["retrieved_at"]
                ):
                    self.data = data
            except (ValueError, OSError, TypeError, KeyError):
                self.error = "Model catalog cache is invalid; using bundled data."

    def status(self):
        return {
            "revision": self.data["revision"],
            "retrieved_at": self.data.get("retrieved_at"),
            "checked_at": self.checked_at,
            "updating": bool(self.task and not self.task.done()),
            "error": self.error,
            "models": len(self.data["models"]),
        }

    def read(self, *, update=True):
        if update and not self.closed and (self.task is None or self.task.done()):
            self.task = asyncio.create_task(self.refresh())
        return self.data

    async def download(self):
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20), trust_env=False) as session:
            async with session.get(
                URL,
                headers={"Accept": "application/json", "User-Agent": "Personal-Agent/model-metadata"},
                allow_redirects=False,
            ) as response:
                if response.status != 200:
                    raise ValueError("Catalog HTTP failure")
                raw = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    raw.extend(chunk)
                    if len(raw) > MAX_BYTES:
                        raise ValueError("Model catalog exceeds 16 MiB")
                return await asyncio.to_thread(compile_catalog, bytes(raw))

    def persist(self, data):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        system.private(self.path.parent, directory=True)
        system.atomic_write(self.path, (json.dumps(data, ensure_ascii=False) + "\n").encode())

    async def refresh(self):
        try:
            data = validate_cache(await self.download())
            await asyncio.to_thread(self.persist, data)
        except (ValueError, TypeError, KeyError, AttributeError, OSError, aiohttp.ClientError, TimeoutError):
            self.error = "Model catalog update failed; existing data remains available."
        else:
            self.data, self.error = data, None
        finally:
            self.checked_at = datetime.now(timezone.utc).isoformat()

    async def close(self):
        self.closed = True
        if self.task and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
