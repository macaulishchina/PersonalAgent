"""One immutable startup snapshot; explicit protocols, capabilities and provider switches."""

from dataclasses import dataclass, field
import hashlib
import json
import re
from copy import deepcopy
from urllib.parse import urlsplit

PROTOCOLS = {"openai_responses", "openai_chat", "anthropic", "gemini"}


PRESETS = {
    "openai": {
        "protocol": "openai_responses",
        "base_url": "https://api.openai.com/v1",
        "key_env": "OPENAI_API_KEY",
    },
    "claude": {
        "protocol": "anthropic",
        "base_url": "https://api.anthropic.com",
        "key_env": "ANTHROPIC_API_KEY",
    },
    "gemini": {
        "protocol": "gemini",
        "base_url": "https://generativelanguage.googleapis.com/v1beta",
        "key_env": "GEMINI_API_KEY",
    },
    "deepseek": {
        "protocol": "openai_chat",
        "base_url": "https://api.deepseek.com/v1",
        "key_env": "DEEPSEEK_API_KEY",
    },
}


# Named third-party presets; never silently substitute their URLs for official ones.
for _suffix, _protocol, _path in (
    ("responses", "openai_responses", "openai/v1"),
    ("chat", "openai_chat", "openai/v1"),
    ("anthropic", "anthropic", "anthropic"),
    ("gemini", "gemini", "google/v1beta"),
):
    PRESETS["opencode_go_" + _suffix] = {
        "protocol": _protocol,
        "base_url": "https://opencode.ai/inference/go/" + _path,
        "key_env": "OPENCODE_API_KEY",
        "auth_scheme": "bearer",
        "session_header": "x-opencode-session",
    }


def keys(value, allowed):
    if not isinstance(value, dict) or set(value) - set(allowed):
        raise ValueError("Unknown model configuration fields")


def name(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", value):
        raise ValueError("Invalid model/provider configuration name")
    return value


def url(value, *, local=False):
    if not isinstance(value, str):
        raise ValueError("Model endpoint must be a URL")
    p = urlsplit(value)
    if p.username or p.password or p.query or p.fragment or not p.hostname:
        raise ValueError("Endpoint cannot contain credentials, query or fragment")
    _ = p.port  # Reject malformed/out-of-range ports during config validation.
    loopback = p.hostname in {"127.0.0.1", "localhost", "::1"}
    if (local and not loopback) or p.scheme not in ({"http", "https"} if loopback else {"https"}):
        raise ValueError("Gateway must be loopback; remote model endpoints require HTTPS")
    return value.rstrip("/")


@dataclass(frozen=True)
class ModelSettings:
    enabled: bool = False
    gateway_url: str = "http://127.0.0.1:4010/v1"
    gateway_key_env: str = "ASSISTANT_MODEL_GATEWAY_KEY"
    concurrency: int = 4
    queue_size: int = 16
    timeout_seconds: int = 60
    max_output_tokens: int = 2048
    providers: dict = field(default_factory=dict)
    entries: dict = field(default_factory=dict)
    memory: str = ""

    @classmethod
    def parse(cls, value):
        keys(value, cls.__dataclass_fields__)
        value = deepcopy(value)
        if "gateway_url" in value:
            value["gateway_url"] = url(value["gateway_url"], local=True)
        providers = value.get("providers", {})
        if not isinstance(providers, dict):
            raise ValueError("Model providers must be a table")
        for ident, provider in providers.items():
            if not isinstance(provider, dict):
                raise ValueError("Provider must be a table")
            preset = provider.get("preset")
            if preset is not None:
                if preset not in PRESETS:
                    raise ValueError("Unknown provider preset")
                keys(provider, {"preset", "key_env", "enabled"})
                providers[ident] = {**PRESETS[preset], **{k: v for k, v in provider.items() if k != "preset"}}
        obj = cls(**value)
        if type(obj.enabled) is not bool:
            raise ValueError("models.enabled must be boolean")
        url(obj.gateway_url, local=True)
        if not re.fullmatch(r"[A-Z][A-Z0-9_]{0,127}", obj.gateway_key_env):
            raise ValueError("Invalid gateway credential reference")
        for key, maximum in (
            ("concurrency", 16),
            ("queue_size", 64),
            ("timeout_seconds", 80),
            ("max_output_tokens", 16384),
        ):
            n = getattr(obj, key)
            if type(n) is not int or not 1 <= n <= maximum:
                raise ValueError("Invalid models." + key)
        if not isinstance(obj.providers, dict) or not isinstance(obj.entries, dict):
            raise ValueError("Model providers and entries must be tables")
        if len(obj.providers) > 16 or len(obj.entries) > 64:
            raise ValueError("Model registry exceeds configured bounds")
        for ident, p in obj.providers.items():
            name(ident)
            keys(
                p,
                {"protocol", "base_url", "key_env", "enabled", "auth_scheme", "session_header"},
            )
            if p.get("auth_scheme", "native") not in {"native", "bearer"}:
                raise ValueError("Unsupported provider authentication scheme")
            if "session_header" in p and not re.fullmatch(r"x-[a-z0-9-]{1,60}", p["session_header"]):
                raise ValueError("Invalid provider session header")
            if p.get("session_header") in {"x-api-key", "x-goog-api-key"}:
                raise ValueError("Session header cannot replace authentication")
            if p.get("protocol") not in PROTOCOLS:
                raise ValueError("Unsupported model protocol")
            p["base_url"] = url(p.get("base_url"))
            if not isinstance(p.get("key_env"), str) or not re.fullmatch(
                r"[A-Z][A-Z0-9_]{0,127}", p["key_env"]
            ):
                raise ValueError("Provider requires a credential environment reference")
            if type(p.get("enabled", False)) is not bool:
                raise ValueError("Provider enabled must be boolean")
        for ident, m in obj.entries.items():
            name(ident)
            keys(
                m,
                {
                    "provider",
                    "model",
                    "images",
                    "structured",
                    "execution_validated",
                    "reasoning_efforts",
                    "max_output_tokens",
                    "context_window",
                    "fallbacks",
                },
            )
            if m.get("provider") not in obj.providers:
                raise ValueError("Unknown provider for " + ident)
            if not isinstance(m.get("model"), str) or not 1 <= len(m["model"]) <= 200:
                raise ValueError("Missing upstream model ID")
            for flag in ("images", "structured", "execution_validated"):
                if type(m.get(flag, False)) is not bool:
                    raise ValueError("Model capabilities must be boolean")
            efforts = m.get("reasoning_efforts", ["none"])
            if (
                not isinstance(efforts, list)
                or not efforts
                or any(
                    e not in {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}
                    for e in efforts
                )
            ):
                raise ValueError("Invalid reasoning efforts")
            window = m.get("context_window", 32768)
            if type(window) is not int or not 8192 <= window <= 2000000:
                raise ValueError("Invalid model context window")
            limit = m.get("max_output_tokens", obj.max_output_tokens)
            if type(limit) is not int or not 1 <= limit <= 16384:
                raise ValueError("Invalid output token budget")
        for ident, entry in obj.entries.items():
            fallback = entry.get("fallbacks", [])
            if (
                not isinstance(fallback, list)
                or len(fallback) > 3
                or any(not isinstance(m, str) or m not in obj.entries for m in fallback)
                or len(set(fallback)) != len(fallback)
                or ident in fallback
            ):
                raise ValueError("Invalid ordered model fallbacks")
        if obj.memory and (obj.memory not in obj.entries or not obj.entries[obj.memory].get("structured")):
            raise ValueError("Memory requires a configured structured-output model")
        if obj.memory and not obj.enabled:
            raise ValueError("Memory API binding requires models.enabled")
        if obj.memory:
            obj.entry(obj.memory)
        return obj

    def entry(self, ident):
        if not self.enabled or ident not in self.entries:
            raise ValueError("Model configuration unavailable")
        m = self.entries[ident]
        if not self.providers[m["provider"]].get("enabled", False):
            raise ValueError("Model provider is disabled")
        return m

    def fingerprint(self):
        return hashlib.sha256(json.dumps(self.__dict__, sort_keys=True).encode()).hexdigest()

    def catalog(self):
        return [
            {
                "id": k,
                "model": k,
                "displayName": k,
                "provider": m["provider"],
                "available": self.enabled and self.providers[m["provider"]].get("enabled", False),
                "images": m.get("images", False),
                "structured": m.get("structured", False),
                "execution_validated": m.get("execution_validated", False),
                "fallbacks": m.get("fallbacks", []),
                "supportedReasoningEfforts": [
                    {"reasoningEffort": e} for e in m.get("reasoning_efforts", ["none"])
                ],
            }
            for k, m in self.entries.items()
        ]
