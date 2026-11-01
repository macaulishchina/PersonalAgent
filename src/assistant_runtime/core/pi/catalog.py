"""Derive Pi model definitions from the sole model registry."""

import json
import os

MODEL_SESSION_HEADER = "x-assistant-session"


def pi_models_config(settings, *, session_id=None, transport_url=""):
    models = {}
    if settings and getattr(settings, "entries", None):
        for ident, entry in settings.entries.items():
            provider_cfg = settings.providers.get(entry["provider"], {})
            if not provider_cfg.get("enabled", False):
                continue
            models[ident] = {
                "id": ident,
                "name": ident,
                "api": "assistant-native",
                "reasoning": any(e != "none" for e in entry["reasoning_efforts"]),
                "thinkingLevelMap": {
                    ("off" if level == "none" else level): (
                        level if level == "none" or level in entry["reasoning_efforts"] else None
                    )
                    for level in ("none", "minimal", "low", "medium", "high", "xhigh", "max")
                },
                "samplingParamsByThinkingLevel": {"off": {"reasoning": {"effort": "none"}}},
                "input": ["text", "image"] if entry.get("images") else ["text"],
                "contextWindow": entry.get("context_window", 32768),
                "maxTokens": entry.get("output_limit") or 131072,
                "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
            }
    return {
        "providers": {
            "assistant_api": {
                "name": "Assistant API",
                "baseUrl": transport_url,
                "api": "openai-responses",
                "apiKey": "$PA_EXECUTION_TOKEN",
                "authHeader": True,
                **({"headers": {MODEL_SESSION_HEADER: session_id}} if session_id else {}),
                "models": list(models.values()),
            }
        }
    }


def write_pi_models(folder, settings, *, session_id=None, transport_url=""):
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = folder / "models.json"
    fd = os.open(path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(pi_models_config(settings, session_id=session_id, transport_url=transport_url), stream)
    return str(path)
