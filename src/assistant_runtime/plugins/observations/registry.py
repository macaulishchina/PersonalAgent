"""Versioned non-secret instances. Only registered adapters can be instantiated."""

from assistant_runtime.api.system import system

import json
from assistant_runtime.api import identifier


class Registry:
    def __init__(self, root, providers):
        self.path = root / "config/observations/sources.json"
        self.providers = providers
        self.revision = 0
        self.sources = {}

    def load(self):
        if self.path.exists():
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if raw.get("version") != 1 or type(raw.get("revision")) is not int or len(raw["sources"]) > 64:
                raise ValueError("Invalid event source registry")
            self.sources = {identifier(key): self.validate(value) for key, value in raw["sources"].items()}
            self.validate_count(self.sources)
            self.revision = raw["revision"]
        return self.describe()

    def validate(self, value):
        if (
            not isinstance(value, dict)
            or set(value) != {"adapter", "config", "enabled"}
            or type(value["enabled"]) is not bool
        ):
            raise ValueError("Source requires adapter/config/enabled")
        if value["adapter"] not in self.providers:
            raise ValueError("Unknown trusted observation adapter")
        return {**value, "config": self.providers[value["adapter"]].validate(value["config"])}

    def validate_count(self, sources):
        if len(sources) > 64:
            raise ValueError("Source count limit exceeded")
        for name, provider in self.providers.items():
            if sum(v["adapter"] == name for v in sources.values()) > provider.max_instances:
                raise ValueError("Adapter instance limit exceeded")

    def describe(self):
        return {"version": 1, "revision": self.revision, "sources": self.sources}

    def update(self, action, ident, revision, definition=None, enabled=None):
        identifier(ident)
        if type(revision) is not int or revision != self.revision:
            raise ValueError("Source registry revision conflict")
        sources = dict(self.sources)
        if action == "configure":
            replacement = self.validate(definition)
            if ident in sources and sources[ident]["adapter"] != replacement["adapter"]:
                raise ValueError("Source identity cannot change adapter")
            sources[ident] = replacement
        elif action == "enable":
            if type(enabled) is not bool:
                raise ValueError("enabled must be boolean")
            sources[ident] = dict(sources[ident], enabled=enabled)
        elif action == "remove":
            sources.pop(ident)
        else:
            raise ValueError("Unknown source management action")
        self.validate_count(sources)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        system.atomic_write(
            self.path,
            (
                json.dumps(
                    {"version": 1, "revision": revision + 1, "sources": sources},
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n"
            ).encode(),
            prefix=".sources-",
        )
        self.sources, self.revision = sources, revision + 1
        return self.describe()
