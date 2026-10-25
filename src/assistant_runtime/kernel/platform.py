"""Immutable platform identity available to every runtime context."""

from dataclasses import dataclass
import sys


@dataclass(frozen=True)
class PlatformInfo:
    name: str

    @classmethod
    def current(cls):
        return cls("windows" if sys.platform == "win32" else sys.platform)
