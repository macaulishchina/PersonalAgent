"""Provider-neutral MCP transport and usage policy validation."""

import copy
from assistant_runtime.api.deadlines import MAX_OPERATION_SECONDS
from assistant_runtime.api.configuration import configuration_default

from dataclasses import dataclass, field
from pathlib import Path
import re
from urllib.parse import urlsplit

CONTEXTS = {"foreground", "background"}
APPROVALS = {"prompt", "approve", "reject"}
NAME = re.compile(r"[a-z][a-z0-9_]{0,47}")
ENV = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def name(value):
    if not isinstance(value, str) or not NAME.fullmatch(value):
        raise ValueError("MCP name must be a lowercase identifier, at most 48 characters")
    return value


def strings(value, label, maximum=100, *, unique=True, empty=False):
    if (
        not isinstance(value, list)
        or len(value) > maximum
        or any(not isinstance(v, str) or (not empty and not v) or len(v) > 2000 or "\0" in v for v in value)
    ):
        raise ValueError("Invalid MCP " + label)
    if unique and len(set(value)) != len(value):
        raise ValueError("Duplicate MCP " + label)
    return list(value)


def mapping(value, label):
    if (
        not isinstance(value, dict)
        or len(value) > 100
        or any(
            not isinstance(k, str) or not k or not isinstance(v, str) or len(v) > 8000 or "\0" in k + v
            for k, v in value.items()
        )
    ):
        raise ValueError("Invalid MCP " + label)
    return dict(value)


@dataclass(frozen=True)
class IntegrationSettings:
    registry_path: str = field(default_factory=lambda: configuration_default(__file__, "registry_path"))
    max_servers: int = field(default_factory=lambda: configuration_default(__file__, "max_servers"))
    inherit_user_servers: bool = field(
        default_factory=lambda: configuration_default(__file__, "inherit_user_servers")
    )

    @classmethod
    def parse(cls, data):
        if not isinstance(data, dict) or set(data) - cls.__dataclass_fields__.keys():
            raise ValueError("Unknown external_mcp settings")
        result = cls(**data)
        if type(result.max_servers) is not int or not 1 <= result.max_servers <= 64:
            raise ValueError("external_mcp.max_servers must be 1..64")
        if type(result.inherit_user_servers) is not bool:
            raise ValueError("inherit_user_servers must be boolean")
        if (
            not isinstance(result.registry_path, str)
            or "\0" in result.registry_path
            or (result.registry_path and not Path(result.registry_path).is_absolute())
        ):
            raise ValueError("MCP registry_path must be an absolute path")
        return result


def definition(value):
    allowed = {
        "transport",
        "enabled",
        "contexts",
        "allow_tools",
        "deny_tools",
        "approval",
        "tool_approvals",
        "startup_timeout_seconds",
        "tool_timeout_seconds",
    }
    if not isinstance(value, dict) or set(value) - allowed:
        raise ValueError("Unknown MCP definition fields")
    spec = {
        "enabled": True,
        "contexts": ["foreground", "background"],
        "allow_tools": None,
        "deny_tools": [],
        "approval": "prompt",
        "tool_approvals": {},
        "startup_timeout_seconds": 60,
        "tool_timeout_seconds": 600,
        **copy.deepcopy(value),
    }
    if type(spec["enabled"]) is not bool:
        raise ValueError("MCP enabled must be boolean")
    spec["contexts"] = strings(spec["contexts"], "contexts", 2)
    if not set(spec["contexts"]) <= CONTEXTS:
        raise ValueError("MCP contexts must be foreground/background")
    if spec["allow_tools"] is not None:
        spec["allow_tools"] = strings(spec["allow_tools"], "allow_tools")
    spec["deny_tools"] = strings(spec["deny_tools"], "deny_tools")
    if not isinstance(spec["approval"], str) or spec["approval"] not in APPROVALS:
        raise ValueError("Invalid MCP approval policy")
    policies = mapping(spec["tool_approvals"], "tool_approvals")
    if any(v not in APPROVALS for v in policies.values()):
        raise ValueError("Invalid per-tool approval policy")
    for key in ("startup_timeout_seconds", "tool_timeout_seconds"):
        if type(spec[key]) is not int or not 1 <= spec[key] <= MAX_OPERATION_SECONDS:
            raise ValueError("MCP timeout must be 1..3600 seconds")
    t = spec.get("transport")
    if not isinstance(t, dict):
        raise ValueError("MCP requires a transport")
    if t.get("type") == "stdio":
        if set(t) - {"type", "command", "args", "cwd", "env", "env_vars"}:
            raise ValueError("Unknown stdio transport fields")
        if (
            not isinstance(t.get("command"), str)
            or not t["command"].strip()
            or "\0" in t["command"]
            or len(t["command"]) > 2000
        ):
            raise ValueError("MCP requires an executable command, not a shell command")
        if t.get("cwd") is not None and (
            not isinstance(t["cwd"], str) or "\0" in t["cwd"] or not Path(t["cwd"]).is_absolute()
        ):
            raise ValueError("MCP cwd must be an absolute path")
        t["args"] = strings(t.get("args", []), "arguments", unique=False, empty=True)
        t["env"] = mapping(t.get("env", {}), "environment")
        t["env_vars"] = strings(t.get("env_vars", []), "environment references")
        if any(not ENV.fullmatch(k) for k in [*t["env"], *t["env_vars"]]):
            raise ValueError("Invalid MCP environment variable name")
    elif t.get("type") == "http":
        if set(t) - {"type", "url", "headers", "env_headers", "bearer_token_env"}:
            raise ValueError("Unknown HTTP transport fields")
        url = t.get("url")
        if not isinstance(url, str) or len(url) > 4000 or any(c.isspace() or ord(c) < 32 for c in url):
            raise ValueError("MCP requires an HTTP(S) URL")
        parsed = urlsplit(url)
        _ = parsed.port  # Validate malformed/non-numeric ports before persisting.
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.fragment
        ):
            raise ValueError("MCP URL must be HTTP(S), without credentials or fragment")
        t["headers"] = mapping(t.get("headers", {}), "HTTP headers")
        t["env_headers"] = mapping(t.get("env_headers", {}), "HTTP environment references")
        if any("\r" in k + v or "\n" in k + v for k, v in {**t["headers"], **t["env_headers"]}.items()):
            raise ValueError("Invalid HTTP header")
        refs = list(t["env_headers"].values()) + ([t["bearer_token_env"]] if "bearer_token_env" in t else [])
        if any(not isinstance(k, str) or not ENV.fullmatch(k) for k in refs):
            raise ValueError("Invalid HTTP credential environment reference")
    else:
        raise ValueError("MCP transport type must be stdio or http (Streamable HTTP)")
    return spec
