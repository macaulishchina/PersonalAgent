"""Generate LiteLLM config without copying any credential into generated files."""

import os
from pathlib import Path


def credential(root, key):
    from dotenv import dotenv_values

    value = os.environ.get(key) or dotenv_values(root / ".env", interpolate=False).get(key)
    if not value or not isinstance(value, str) or "\n" in value or "\r" in value:
        raise ValueError("Missing or invalid model credential: " + key)
    return value


def gateway_credential(root, settings):
    try:
        return credential(root, settings.gateway_key_env)
    except ValueError:
        path = Path(root) / "state/model-api/gateway.key"
        if not path.is_file():
            raise ValueError("Gateway key missing; run scripts.prepare_model_gateway") from None
        value = path.read_text().strip()
        if len(value) < 32 or any(c.isspace() for c in value):
            raise ValueError("Invalid private gateway key")
        return value


def configuration(settings):
    models = []
    for ident, entry in settings.entries.items():
        p = settings.providers[entry["provider"]]
        if not settings.enabled or not p.get("enabled", False):
            continue
        protocol = p["protocol"]
        prefix = {
            "openai_responses": "openai/",
            "openai_chat": "openai/",
            "anthropic": "anthropic/",
            "gemini": "gemini/",
        }[protocol]
        params = {
            "model": prefix + entry["model"],
            "api_base": p["base_url"],
            "api_key": "os.environ/" + p["key_env"],
            "num_retries": 0,
            "timeout": settings.timeout_seconds,
            "max_output_tokens" if protocol == "openai_responses" else "max_tokens": entry.get(
                "max_output_tokens", settings.max_output_tokens
            ),
        }
        if protocol == "openai_chat":
            params["use_chat_completions_api"] = True
        models.append({"model_name": ident, "litellm_params": params})
    return {
        "model_list": models,
        "general_settings": {"master_key": "os.environ/" + settings.gateway_key_env},
        "litellm_settings": {
            "drop_params": False,
            "modify_params": False,
            "num_retries": 0,
            "telemetry": False,
            "set_verbose": False,
            "callbacks": ["gateway_hooks.transport"],
        },
        "router_settings": {
            "num_retries": 0,
            "fallbacks": [],
            "context_window_fallbacks": [],
            "content_policy_fallbacks": [],
            "disable_cooldowns": True,
        },
    }
