"""Versioned public plugin API; provider protocol details stay in adapters."""

from .configuration import ConfigurationSpec
from .files import FileRequest
from .runs import ExecutionRequest
from .conversation import ConversationRequest
from .backends import BackendSpec, PlatformInfo
from .plugins import CallIdentity, PluginContext, PluginDefinition, Tool
from .releases import plugin_manifest
from assistant_runtime.kernel.runtime import Manifest
from assistant_runtime.kernel.event_reporting import identifier

__all__ = [
    "ConfigurationSpec",
    "BackendSpec",
    "PlatformInfo",
    "CallIdentity",
    "PluginContext",
    "PluginDefinition",
    "Tool",
    "Manifest",
    "plugin_manifest",
    "identifier",
    "ExecutionRequest",
    "ConversationRequest",
    "FileRequest",
]
