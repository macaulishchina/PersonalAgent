"""Generate LiteLLM config without copying any credential into generated files."""

from assistant_runtime.api.configuration_io import configuration_path

import os
from pathlib import Path


def credential(root, key):
    from dotenv import dotenv_values

    value = os.environ.get(key) or dotenv_values(configuration_path(root / ".env"), interpolate=False).get(
        key
    )
    if not value or not isinstance(value, str) or "\n" in value or "\r" in value:
        raise ValueError("Missing or invalid model credential: " + key)
    return value


def gateway_credential(root, settings):
    if settings.entries and all(
        settings.providers[e["provider"]]["protocol"] == "pi" for e in settings.entries.values()
    ):
        # Native-only instances still authenticate their private Pi loopback bridge.
        # This process-local token is not a supplier credential or an external gateway prerequisite.
        import secrets

        return secrets.token_urlsafe(32)
    try:
        return credential(root, settings.gateway_key_env)
    except ValueError:
        path = configuration_path(Path(root) / "state/model-api/gateway.key")
        if not path.is_file():
            raise ValueError("Gateway key missing; run scripts.prepare_model_gateway") from None
        value = path.read_text(encoding="utf-8").strip()
        if len(value) < 32 or any(c.isspace() for c in value):
            raise ValueError("Invalid private gateway key")
        return value


def configuration(settings):
    models = []
    for ident, entry in settings.entries.items():
        p = settings.providers[entry["provider"]]
        if not settings.enabled or not p.get("enabled", False) or p["protocol"] == "pi":
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
            "timeout": settings.outer_timeout_seconds,
            "max_output_tokens" if protocol == "openai_responses" else "max_tokens": entry.get(
                "execution_max_output_tokens", settings.execution_max_output_tokens
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


def application_steps(previous, current, previous_resources=None, current_resources=None):
    """A worker restart cannot apply generated assets to a separate gateway process."""
    target = configuration(current)
    if not target["model_list"]:
        return []
    credentials_changed = False
    if previous_resources is not None and current_resources is not None:
        from dotenv import dotenv_values
        from io import StringIO

        before = dotenv_values(
            stream=StringIO((previous_resources.get("credentials") or b"").decode()), interpolate=False
        )
        after = dotenv_values(
            stream=StringIO((current_resources.get("credentials") or b"").decode()), interpolate=False
        )
        names = {
            p["key_env"] for p in current.providers.values() if p["protocol"] != "pi" and p.get("enabled")
        }
        names.add(current.gateway_key_env)
        credentials_changed = any(before.get(k) != after.get(k) for k in names) or previous_resources.get(
            "gateway_key"
        ) != current_resources.get("gateway_key")
    if previous is not None and configuration(previous) == target and not credentials_changed:
        return []
    return [
        {
            "component": "model_gateway",
            "state": "manual_action_required",
            "message": "模型网关配置或凭据需确认；请按需重新生成并部署网关配置，业务重启不能代替此步骤。",
        }
    ]
