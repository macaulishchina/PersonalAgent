"""Generate private, derived Docker inputs from one instance configuration; no paid calls."""

import argparse
import hashlib
import json
import os
import secrets
from pathlib import Path
from urllib.parse import urlsplit

from assistant_runtime.config import Config
from assistant_runtime.plugins.models.settings import ModelSettings, reasoning_effort
from assistant_runtime.plugins.models.gateway import configuration, credential, gateway_credential

# Pulled from the official repository; independently checked package version: 1.106.0.
IMAGE = "ghcr.io/berriai/litellm@sha256:2e4c0371f3a8c28e4da131d77abaa8341cd3b0c2fcc4d34d7c5900431af7b9d0"


def private_write(path, value):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(value)
    path.chmod(0o600)


def prepare(config):
    settings = ModelSettings.parse(config.model_api)
    if not settings.enabled:
        raise ValueError("Model API is disabled")
    parsed = urlsplit(settings.gateway_url)
    if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or parsed.path != "/v1":
        raise ValueError("Bundled Docker deployment requires http://127.0.0.1:PORT/v1")
    folder = config.state_dir / "model-api"
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    folder.chmod(0o700)
    try:
        key = gateway_credential(config.root, settings)
    except ValueError:
        key = "sk-" + secrets.token_urlsafe(36)
        private_write(folder / "gateway.key", key)
    environment = {
        settings.gateway_key_env: key,
        "LITELLM_TELEMETRY": "False",
        "LITELLM_LOG": "ERROR",
        "STORE_MODEL_IN_DB": "False",
        "LITELLM_LOCAL_MODEL_COST_MAP": "True",
        "ASSISTANT_GATEWAY_ROUTES": "/etc/litellm/routes.json",
        "PYTHONPATH": "/etc/litellm",
    }
    for p in settings.providers.values():
        if p.get("enabled", False) and p["protocol"] != "pi":
            environment[p["key_env"]] = credential(config.root, p["key_env"])
    # JSON Compose keeps literal values, including $, out of shell interpolation.
    # Only referenced credentials enter this private file; never copy the complete .env.
    manifest = {
        "name": "assistant-model-" + hashlib.sha256(str(config.root).encode()).hexdigest()[:10],
        "services": {
            "gateway": {
                "image": IMAGE,
                "entrypoint": ["python", "-c", "from litellm import run_server; run_server()"],
                "restart": "unless-stopped",
                "ports": [f"127.0.0.1:{parsed.port or 80}:4000"],
                "environment": {k: v.replace("$", "$$") for k, v in environment.items()},
                "volumes": [f"{folder / 'gateway'}:/etc/litellm:ro"],
                "command": [
                    "--config",
                    "/etc/litellm/config.yaml",
                    "--port",
                    "4000",
                    "--host",
                    "0.0.0.0",
                    "--num_workers",
                    "1",
                ],
                "mem_limit": "2g",
                "cpus": 2,
                "pids_limit": 256,
                "logging": {"driver": "local", "options": {"max-size": "5m", "max-file": "2"}},
            }
        },
    }
    assets = folder / "gateway"
    assets.mkdir(mode=0o700, exist_ok=True)
    private_write(assets / "config.yaml", json.dumps(configuration(settings), indent=2))
    routes = {
        ident: {
            **settings.providers[entry["provider"]],
            "reasoning_efforts": entry["reasoning_efforts"],
            "default_reasoning_effort": reasoning_effort(entry),
        }
        for ident, entry in settings.entries.items()
        if settings.providers[entry["provider"]].get("enabled", False)
        and settings.providers[entry["provider"]]["protocol"] != "pi"
    }
    from assistant_runtime.plugins.pi.catalog import MODEL_SESSION_HEADER

    private_write(
        assets / "routes.json",
        json.dumps({"providers": routes, "session_headers": ["x-assistant-session", MODEL_SESSION_HEADER]}),
    )
    from assistant_runtime.plugins.models import gateway_transport

    private_write(assets / "gateway_transport.py", Path(gateway_transport.__file__).read_text())
    private_write(
        assets / "gateway_hooks.py", "from gateway_transport import callback\ntransport = callback()\n"
    )
    private_write(folder / "compose.json", json.dumps(manifest, indent=2))
    private_write(folder / "revision", settings.fingerprint() + "\n")
    return folder / "compose.json"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    print(prepare(Config.load(args.config)))
    print(
        "Generated only; start/recreate the container explicitly, then restart the assistant after acceptance."
    )


if __name__ == "__main__":
    main()
