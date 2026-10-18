"""Small transport-only callback, copied into the isolated LiteLLM container.

No execution, routing or credentials in this source. Only declared authentication
and a hashed conversation identity can cross the gateway; client headers cannot.
"""

import hashlib
import json
import os
from pathlib import Path


def upstream_headers(provider, incoming, environment, session_headers=("x-assistant-session",)):
    headers = {"User-Agent": "personal-agent-model-gateway/1.0"}
    if provider.get("auth_scheme") == "bearer":
        key = environment[provider["key_env"]]
        if not key or "\n" in key or "\r" in key:
            raise ValueError("Invalid upstream credential")
        headers["Authorization"] = "Bearer " + key
    if target := provider.get("session_header"):
        incoming = {k.lower(): v for k, v in incoming.items()}
        # Auxiliary inference binds identity server-side; Codex supplies its native
        # thread header. Never invent a new session for every model turn.
        session = next((incoming[h] for h in session_headers if incoming.get(h)), None)
        if not isinstance(session, str) or not 1 <= len(session) <= 256:
            raise ValueError("Provider requires a bounded conversation identity")
        headers[target] = hashlib.sha256(session.encode()).hexdigest()
    return headers


def prepare_request(provider, data, environment, session_headers):
    incoming = data.get("proxy_server_request", {}).get("headers", {})
    data.pop("headers", None)
    data["extra_headers"] = upstream_headers(provider, incoming, environment, session_headers)
    if provider["protocol"] in {"anthropic", "gemini"}:
        # OpenAI's cache-routing hint has no Messages/Gemini equivalent. It does not
        # change model input or execution semantics. Keep all tools, reasoning,
        # image and output-schema parameters subject to strict validation.
        data.pop("prompt_cache_key", None)
    return data


def callback():
    # LiteLLM is deliberately installed only in its own container.
    from litellm.integrations.custom_logger import CustomLogger

    config = json.loads(Path(os.environ["ASSISTANT_GATEWAY_ROUTES"]).read_text())
    routes = config["providers"]

    class Transport(CustomLogger):
        async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
            provider = routes.get(data.get("model"))
            if provider is None:
                raise ValueError("Unknown configured model route")
            return prepare_request(provider, data, os.environ, config["session_headers"])

    return Transport()
