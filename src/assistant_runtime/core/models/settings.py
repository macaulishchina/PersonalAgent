"""One immutable startup snapshot; explicit protocols, capabilities and provider switches."""

from assistant_runtime.api.deadlines import MAX_OPERATION_SECONDS, TRANSPORT_MARGIN_SECONDS

from assistant_runtime.api.configuration import configuration_default, ConfigurationError

from dataclasses import dataclass, field
import hashlib
import json
import re
from copy import deepcopy
from .capabilities import resolve, output_bound
from urllib.parse import urlsplit

PROTOCOLS = {"openai_responses", "openai_chat", "anthropic", "gemini", "pi"}
EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}


def reasoning_effort(entry, requested=None):
    supported = entry["reasoning_efforts"]
    effort = requested if requested is not None else entry.get("default_reasoning_effort", "auto")
    if not isinstance(effort, str) or effort != "auto" and effort not in supported:
        raise ValueError("Unsupported model reasoning effort")
    return effort


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
for _prefix, _root in (
    ("opencode", "https://opencode.ai/inference"),
    ("opencode_go", "https://opencode.ai/inference/go"),
):
    for _suffix, _protocol, _path in (
        ("responses", "openai_responses", "openai/v1"),
        ("chat", "openai_chat", "openai/v1"),
        ("anthropic", "anthropic", "anthropic"),
        ("gemini", "gemini", "google/v1beta"),
    ):
        PRESETS[_prefix + "_" + _suffix] = {
            "protocol": _protocol,
            "base_url": _root + "/" + _path,
            "key_env": "OPENCODE_API_KEY",
            "auth_scheme": "bearer",
            "session_header": "x-opencode-session",
            "catalog": {"url": _root + "/v1/models", "format": "openai"},
        }


def keys(value, allowed):
    if not isinstance(value, dict) or set(value) - set(allowed):
        raise ValueError("Unknown model configuration fields")


def name(value, path=("model_api",)):
    model_alias = tuple(path) == ("model_api", "entries")
    pattern = r"[A-Za-z][A-Za-z0-9_.-]{0,63}" if model_alias else r"[A-Za-z][A-Za-z0-9_-]{0,63}"
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        raise ConfigurationError(
            path,
            "invalid_registry_name",
            "模型名称须以英文字母开头，可包含字母、数字、点号、下划线或连字符，最多 64 个字符。"
            if model_alias
            else "名称须以英文字母开头，可包含大小写字母、数字、下划线或连字符，最多 64 个字符。",
        )
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
        raise ValueError("Remote model endpoints require HTTPS; local endpoints must use loopback")
    return value.rstrip("/")


@dataclass(frozen=True)
class ModelSettings:
    enabled: bool = field(default_factory=lambda: configuration_default(__file__, "enabled"))
    concurrency: int = field(default_factory=lambda: configuration_default(__file__, "concurrency"))
    queue_size: int = field(default_factory=lambda: configuration_default(__file__, "queue_size"))
    timeout_seconds: int = field(default_factory=lambda: configuration_default(__file__, "timeout_seconds"))
    compaction_timeout_seconds: int = field(
        default_factory=lambda: configuration_default(__file__, "compaction_timeout_seconds")
    )
    tool_result_max_chars: int = field(
        default_factory=lambda: configuration_default(__file__, "tool_result_max_chars")
    )
    tool_schema_max_bytes: int = field(
        default_factory=lambda: configuration_default(__file__, "tool_schema_max_bytes")
    )
    invalid_tool_call_limit: int = field(
        default_factory=lambda: configuration_default(__file__, "invalid_tool_call_limit")
    )
    max_loaded_tools: int = field(default_factory=lambda: configuration_default(__file__, "max_loaded_tools"))
    max_output_tokens: int = field(
        default_factory=lambda: configuration_default(__file__, "max_output_tokens")
    )
    execution_max_output_tokens: int = field(
        default_factory=lambda: configuration_default(__file__, "execution_max_output_tokens")
    )
    providers: dict = field(default_factory=lambda: configuration_default(__file__, "providers"))
    accounts: dict = field(default_factory=lambda: configuration_default(__file__, "accounts"))
    entries: dict = field(default_factory=lambda: configuration_default(__file__, "entries"))
    memory: str = field(default_factory=lambda: configuration_default(__file__, "memory"))

    retry: dict = field(default_factory=lambda: configuration_default(__file__, "retry"))

    @property
    def outer_timeout_seconds(self):
        return max(self.timeout_seconds, self.compaction_timeout_seconds) + TRANSPORT_MARGIN_SECONDS

    @classmethod
    def parse(cls, value):
        keys(value, cls.__dataclass_fields__)
        value = deepcopy(value)
        providers = value.get("providers", {})
        if not isinstance(providers, dict):
            raise ValueError("Model providers must be a table")
        for ident, provider in providers.items():
            if not isinstance(provider, dict):
                raise ValueError("Provider must be a table")
            preset = provider.get("preset")
            if "account" in provider:
                keys(provider, {"account", "enabled"})
                provider["protocol"] = "pi"
            if preset is not None:
                if preset not in PRESETS:
                    raise ValueError("Unknown provider preset")
                keys(provider, {"preset", "key_env", "enabled"})
                providers[ident] = {
                    **deepcopy(PRESETS[preset]),
                    **{k: v for k, v in provider.items() if k != "preset"},
                }
        obj = cls(**value)
        defaults = configuration_default(__file__, "retry")
        keys(obj.retry, defaults)
        obj.retry.update({k: v for k, v in defaults.items() if k not in obj.retry})
        for key, maximum in (("max_retries", 5), ("unknown_retries", 1)):
            if type(obj.retry[key]) is not int or not 0 <= obj.retry[key] <= maximum:
                raise ValueError("Invalid models.retry." + key)
        for key, minimum, maximum in (("max_delay_seconds", 1, 600),):
            if type(obj.retry[key]) not in (int, float) or not minimum <= obj.retry[key] <= maximum:
                raise ValueError("Invalid models.retry." + key)
        if type(obj.enabled) is not bool:
            raise ValueError("models.enabled must be boolean")
        for key, maximum in (
            ("concurrency", 16),
            ("queue_size", 64),
            ("timeout_seconds", MAX_OPERATION_SECONDS),
            ("compaction_timeout_seconds", MAX_OPERATION_SECONDS),
            ("tool_result_max_chars", 50000),
            ("max_loaded_tools", 64),
            ("tool_schema_max_bytes", 1048576),
            ("invalid_tool_call_limit", 32),
            ("max_output_tokens", 16384),
            ("execution_max_output_tokens", 131072),
        ):
            n = getattr(obj, key)
            minimum = {
                "max_output_tokens": 0,
                "execution_max_output_tokens": 0,
                "tool_schema_max_bytes": 1024,
                "invalid_tool_call_limit": 2,
            }.get(key, 1)
            if type(n) is not int or not minimum <= n <= maximum:
                raise ValueError("Invalid models." + key)
        if not isinstance(obj.providers, dict) or not isinstance(obj.entries, dict):
            raise ValueError("Model providers and entries must be tables")
        if len(obj.providers) > 16 or len(obj.entries) > 64:
            raise ValueError("Model registry exceeds configured bounds")
        if not isinstance(obj.accounts, dict) or len(obj.accounts) > 16:
            raise ValueError("Invalid native model accounts")
        for registry in ("accounts", "providers", "entries"):
            seen = set()
            for ident in getattr(obj, registry):
                name(ident, ("model_api", registry))
                if ident.casefold() in seen:
                    raise ConfigurationError(
                        ["model_api", registry],
                        "duplicate_registry_name",
                        "同一列表中的名称不能仅以大小写区分，请使用不同的名称。",
                    )
                seen.add(ident.casefold())
        for ident, account in obj.accounts.items():
            keys(account, {"provider", "auth", "key_env", "output_limit"})
            name(account.get("provider"), ("model_api", "accounts", ident, "provider"))
            if account.get("auth") not in {"oauth", "api_key"}:
                raise ValueError("Native account requires explicit oauth or api_key authentication")
            if "key_env" in account and (
                account["auth"] != "api_key"
                or not isinstance(account["key_env"], str)
                or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,127}", account["key_env"])
            ):
                raise ValueError("Invalid native account credential reference")
            if account.get("output_limit", "request") not in {"request", "model"}:
                raise ValueError("Invalid native output limit mode")
            if account["provider"] == "openai" and account["auth"] == "oauth":
                if account.get("output_limit") != "model":
                    raise ValueError(
                        "ChatGPT subscription requires explicit output_limit='model'; request caps unsupported"
                    )
        for ident, p in obj.providers.items():
            if p.get("protocol") == "pi":
                keys(p, {"protocol", "account", "enabled"})
                if p.get("account") not in obj.accounts or type(p.get("enabled", False)) is not bool:
                    raise ValueError("Invalid native account binding")
                continue
            keys(
                p,
                {"protocol", "base_url", "key_env", "enabled", "auth_scheme", "session_header", "catalog"},
            )
            if p.get("auth_scheme", "native") not in {"native", "bearer"}:
                raise ValueError("Unsupported provider authentication scheme")
            if (
                p.get("session_header") != ""
                and "session_header" in p
                and not re.fullmatch(r"x-[a-z0-9-]{1,60}", p["session_header"])
            ):
                raise ValueError("Invalid provider session header")
            if p.get("session_header") in {"x-api-key", "x-goog-api-key"}:
                raise ValueError("Session header cannot replace authentication")
            if p.get("protocol") not in PROTOCOLS:
                raise ValueError("Unsupported model protocol")
            p["base_url"] = url(p.get("base_url"))
            if "catalog" in p:
                catalog = p["catalog"]
                keys(catalog, {"url", "format"})
                catalog["url"] = url(catalog.get("url"))
                if catalog.get("format") not in {"openai", "anthropic", "gemini"}:
                    raise ValueError("Unsupported catalog format")
                base, directory = urlsplit(p["base_url"]), urlsplit(catalog["url"])
                default_port = 443 if base.scheme == "https" else 80
                if (base.scheme, base.hostname, base.port or default_port) != (
                    directory.scheme,
                    directory.hostname,
                    directory.port or default_port,
                ):
                    raise ValueError("Catalog URL must use the same origin as the inference endpoint")
            if not isinstance(p.get("key_env"), str) or not re.fullmatch(
                r"[A-Z][A-Z0-9_]{0,127}", p["key_env"]
            ):
                raise ValueError("Provider requires a credential environment reference")
            if type(p.get("enabled", False)) is not bool:
                raise ValueError("Provider enabled must be boolean")
        for ident, m in obj.entries.items():
            keys(
                m,
                {
                    "provider",
                    "model",
                    "images",
                    "structured",
                    "reasoning_efforts",
                    "default_reasoning_effort",
                    "max_output_tokens",
                    "execution_max_output_tokens",
                    "context_window",
                    "output_limit",
                    "capabilities",
                },
            )
            if m.get("provider") not in obj.providers:
                raise ValueError("Unknown provider for " + ident)
            if not isinstance(m.get("model"), str) or not 1 <= len(m["model"]) <= 200:
                raise ValueError("Missing upstream model ID")
            provider = obj.providers[m["provider"]]
            try:
                resolve(m, provider, obj.accounts.get(provider.get("account")))
            except ValueError:
                raise ConfigurationError(
                    ["model_api", "entries", ident, "capabilities"],
                    "model_capabilities_invalid",
                    "Invalid or changed model capability snapshot；连接或模型已变化，请刷新能力资料。",
                ) from None
            for flag in ("images", "structured"):
                if type(m.get(flag, False)) is not bool:
                    raise ValueError("Model capabilities must be boolean")
            efforts = m.get("reasoning_efforts")
            if (
                not isinstance(efforts, list)
                or any(not isinstance(e, str) or e not in EFFORTS for e in efforts)
                or len(set(efforts)) != len(efforts)
            ):
                raise ValueError("Invalid reasoning efforts")
            try:
                reasoning_effort(m)
            except ValueError:
                raise ConfigurationError(
                    ["model_api", "entries", ident, "default_reasoning_effort"],
                    "reasoning_unsupported",
                    "Unsupported model reasoning effort；请选择当前模型支持的档位或自动。",
                ) from None
            window = m.get("context_window", 32768)
            if type(window) is not int or not 1 <= window <= 10000000:
                raise ValueError("Invalid model context window")
            output_limit = m.get("output_limit")
            if output_limit is not None and (
                type(output_limit) is not int or not 1 <= output_limit <= 10000000
            ):
                raise ValueError("Invalid model output limit")
            limit = m.get("max_output_tokens", obj.max_output_tokens)
            if type(limit) is not int or not 0 <= limit <= 16384:
                raise ValueError("Invalid output token budget")
            execution_limit = m.get("execution_max_output_tokens", obj.execution_max_output_tokens)
            if type(execution_limit) is not int or not 0 <= execution_limit <= 131072:
                raise ValueError("Invalid execution output token budget")
            m["max_output_tokens"] = output_bound(m, limit)
            m["execution_max_output_tokens"] = output_bound(m, execution_limit)
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
                "default_reasoning_effort": reasoning_effort(m),
                "supported_reasoning_efforts": [
                    {"reasoning_effort": e} for e in ["auto", *m["reasoning_efforts"]]
                ],
                "capabilities": deepcopy(m["capabilities"]),
            }
            for k, m in self.entries.items()
        ]
