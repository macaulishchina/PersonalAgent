"""Startup-only search instances. Credentials are environment references."""

from assistant_runtime.api.configuration import configuration_default

from dataclasses import dataclass, field
import re
from urllib.parse import urlsplit


@dataclass(frozen=True)
class SearchSettings:
    default_provider: str = field(default_factory=lambda: configuration_default(__file__, "default_provider"))
    providers: dict = field(default_factory=lambda: configuration_default(__file__, "providers"))
    timeout_seconds: int = field(default_factory=lambda: configuration_default(__file__, "timeout_seconds"))
    concurrency: int = field(default_factory=lambda: configuration_default(__file__, "concurrency"))
    max_pending: int = field(default_factory=lambda: configuration_default(__file__, "max_pending"))
    cache_seconds: int = field(default_factory=lambda: configuration_default(__file__, "cache_seconds"))
    requests_per_second: int = field(
        default_factory=lambda: configuration_default(__file__, "requests_per_second")
    )

    @classmethod
    def parse(cls, data):
        if not isinstance(data, dict) or set(data) - cls.__dataclass_fields__.keys():
            raise ValueError("Unknown search settings")
        values = dict(data)
        for key, low, high in (
            ("timeout_seconds", 1, 60),
            ("concurrency", 1, 16),
            ("max_pending", 1, 64),
            ("cache_seconds", 0, 300),
            ("requests_per_second", 1, 100),
        ):
            v = values.get(key, configuration_default(__file__, key))
            if type(v) is not int or not low <= v <= high:
                raise ValueError("Invalid search " + key)
        providers = data.get("providers", {})
        if not isinstance(providers, dict) or len(providers) > 16:
            raise ValueError("Invalid search provider registry")
        parsed = {}
        for name, item in providers.items():
            if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", name):
                raise ValueError("Invalid search provider name")
            if not isinstance(item, dict) or set(item) - {"adapter", "base_url", "api_key_env"}:
                raise ValueError("Unknown search provider fields")
            if item.get("adapter", "deepseek") != "deepseek":
                raise ValueError("Unsupported search adapter")
            endpoint = item.get("base_url", "https://api.deepseek.com")
            if not isinstance(endpoint, str) or len(endpoint) > 2000 or any(ord(c) <= 32 for c in endpoint):
                raise ValueError("Invalid search endpoint")
            url = urlsplit(endpoint)
            if (
                url.scheme not in {"http", "https"}
                or not url.hostname
                or url.username
                or url.password
                or url.query
                or url.fragment
            ):
                raise ValueError("Search endpoint must be HTTP(S) without credentials/query/fragment")
            if url.scheme == "http" and url.hostname not in {"127.0.0.1", "::1", "localhost"}:
                raise ValueError("Remote search endpoints require HTTPS")
            try:
                url.port
            except ValueError:
                raise ValueError("Invalid search endpoint port") from None
            env = item.get("api_key_env", "DEEPSEEK_API_KEY")
            if not isinstance(env, str) or not re.fullmatch(r"[A-Z_][A-Z0-9_]{0,127}", env):
                raise ValueError("Search credentials must reference an environment variable")
            parsed[name] = {"adapter": "deepseek", "base_url": endpoint.rstrip("/"), "api_key_env": env}
        default = data.get("default_provider", "")
        if not isinstance(default, str) or (parsed and default not in parsed) or (default and not parsed):
            raise ValueError("Select a configured default search provider")
        values["providers"] = parsed
        result = cls(**values)
        if result.max_pending < result.concurrency:
            raise ValueError("Search max_pending must cover concurrency")
        return result
