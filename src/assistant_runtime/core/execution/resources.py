"""One host-owned admission resource, independent of plugin startup order."""

from assistant_runtime.api.capacity import Capacity
from .declaration import settings


def capacity_for(host):
    value = getattr(host, "execution_capacity", None)
    if value is None:
        value = Capacity(settings(getattr(host.config, "execution", {}))["concurrency"])
        host.execution_capacity = value
    return value
