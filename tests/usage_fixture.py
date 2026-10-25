"""Real isolated metering for plugin fixtures; no paid network requests."""

from pathlib import Path
from types import SimpleNamespace
from assistant_runtime.core.ai_usage.plugin import AIUsagePlugin


def attach_usage(host, runtime):
    if not hasattr(host.config, "state_dir"):
        host.config.state_dir = Path(host.config.root) / "state"
    if not hasattr(host.config, "ai_usage"):
        host.config.ai_usage = {}
    if not hasattr(host.config, "plugin_backends"):
        host.config.plugin_backends = {}
    host.runtime = runtime
    host.ai_usage = AIUsagePlugin(host)
    runtime.add(host.ai_usage)
    return host.ai_usage


def decision_host(config, runtime):
    host = SimpleNamespace(config=config, runtime=runtime)
    attach_usage(host, runtime)
    return host
