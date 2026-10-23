"""Versioned public plugin API; provider protocol details stay in adapters."""

from .files import FileRequest
from .runs import ExecutionRequest
from .plugins import CallIdentity, PluginContext, PluginDefinition, Tool
from assistant_runtime.kernel.runtime import Manifest
from assistant_runtime.kernel.event_reporting import identifier

__all__ = [
    "CallIdentity",
    "PluginContext",
    "PluginDefinition",
    "Tool",
    "Manifest",
    "identifier",
    "ExecutionRequest",
    "FileRequest",
]
