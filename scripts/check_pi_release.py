"""Read-only Pi release preflight. No model calls, device connections or service changes."""

import argparse
import json
from pathlib import Path
import shutil
import subprocess

from assistant_runtime.app.bootstrap import assistant_skill_paths
from assistant_runtime.app.config import Config
from assistant_runtime.core.ai_usage.settings import UsageSettings
from assistant_runtime.core.integrations.registry import McpRegistry
from assistant_runtime.core.models.gateway import credential, gateway_credential
from assistant_runtime.core.models.settings import ModelSettings, reasoning_effort
from assistant_runtime.core.tasks.settings import TaskSettings


def check(config, *, native=True):
    checks = []

    def record(name, verify):
        try:
            detail = verify()
            checks.append({"check": name, "state": "passed", "detail": detail})
        except Exception as exc:
            # Credential errors and subprocess output must never reach the report.
            checks.append({"check": name, "state": "failed", "error": type(exc).__name__})

    settings = ModelSettings.parse(config.model_api)

    def execution():
        required = {"pi", "models", "ai_usage", "voice_disabled"}
        if config.backend != "pi" or not required <= set(config.plugins):
            raise ValueError("Missing required execution plugins")
        if {"codex", "keyword", "browser_speech", "sherpa_speech"} & set(config.plugins):
            raise ValueError("Unaccepted or retired voice/execution path enabled")
        entry = settings.entry(config.main_model)
        if not entry.get("execution_validated"):
            raise ValueError("Main execution model is not accepted")
        reasoning_effort(entry, config.main_reasoning_effort)
        return {"model": config.main_model, "effort": config.main_reasoning_effort}

    def tasks():
        policy = TaskSettings.parse(config.tasks)
        from assistant_runtime.core.tasks.selection import ModelPolicy

        selection = ModelPolicy.parse(policy.model_policy)
        for row in selection.profiles.values():
            entry = settings.entry(row["model"])
            if not entry.get("execution_validated"):
                raise ValueError("Task profile uses an unaccepted executor")
            reasoning_effort(entry, row["reasoning_effort"])
        for candidates in selection.fallbacks.values():
            for row in candidates:
                entry = settings.entry(row["model"])
                if not entry.get("execution_validated"):
                    raise ValueError("Fallback uses an unaccepted executor")
                reasoning_effort(entry, row["reasoning_effort"])
        return {"profiles": list(selection.profiles), "concurrency": policy.concurrency}

    def memory():
        if config.memory_enabled:
            if "memory" not in config.plugins or not settings.memory:
                raise ValueError("Enabled memory needs its plugin and explicit API binding")
            if not settings.entry(settings.memory).get("structured"):
                raise ValueError("Memory requires structured output")
        return {"enabled": config.memory_enabled, "model": settings.memory}

    def secrets():
        for spec in settings.providers.values():
            if spec.get("enabled"):
                if spec["protocol"] == "pi":
                    account = settings.accounts[spec["account"]]
                    if account.get("key_env"):
                        credential(config.root, account["key_env"])
                    else:
                        path = config.state_dir / "model-accounts" / spec["account"] / "auth.json"
                        stored = json.loads(path.read_text()).get(account["provider"], {})
                        required = ("access",) if account["auth"] == "oauth" else ("key",)
                        if stored.get("type") != account["auth"] or any(not stored.get(k) for k in required):
                            raise ValueError("Native model account requires operator login")
                        if account["auth"] == "oauth" and (
                            not isinstance(stored.get("refresh"), str)
                            or type(stored.get("expires")) not in (int, float)
                        ):
                            raise ValueError("Incomplete native OAuth credential")
                else:
                    credential(config.root, spec["key_env"])
        gateway_credential(config.root, settings)
        return "referenced_credentials_present"

    def sources():
        from assistant_runtime.plugins.observations.registry import Registry
        from assistant_runtime.plugins.reyee.events import ReyeeEvents
        from assistant_runtime.plugins.adb_tv.events import TelevisionEvents
        from assistant_runtime.core.speech.wake_events import WakeEvents

        registry = Registry(
            config.root,
            {
                "wake": WakeEvents(None),
                "reyee": ReyeeEvents(config.state_dir / "observations"),
                "adb_tv": TelevisionEvents(),
            },
        )
        registry.load()
        return {"sources": len(registry.sources), "revision": registry.revision}

    def version():
        binary = shutil.which(config.pi_binary)
        if not binary:
            raise FileNotFoundError("Pi executable missing")
        result = subprocess.run([binary, "--version"], capture_output=True, timeout=10, check=True)
        if result.stdout.decode().strip() != "1.4.0":
            raise ValueError("Pi differs from the accepted pinned version")
        return "1.4.0"

    record("execution_binding", execution)
    record("task_profiles", tasks)
    record("memory_binding", memory)
    record("usage_policy", lambda: {"max_requests": UsageSettings.parse(config.ai_usage).max_requests})
    record("assistant_skills", lambda: [p.name for p in assistant_skill_paths(config)])
    if "integrations" in config.plugins:
        record("mcp_registry", lambda: {"revision": McpRegistry(config.root, config.external_mcp).revision})
    if "observations" in config.plugins:
        record("observation_registry", sources)
    record("credential_references", secrets)
    if native:
        record("pinned_pi", version)
    return {
        "state": "passed" if all(row["state"] == "passed" for row in checks) else "failed",
        "scope": "offline_preflight_only",
        "real_provider_acceptance": "separate_required",
        "production_cutover": "not_performed",
        "checks": checks,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    args = parser.parse_args()
    result = check(Config.load(args.config))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result["state"] == "passed" else 1)


if __name__ == "__main__":
    main()
