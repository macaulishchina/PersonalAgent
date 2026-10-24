"""Personal Agent. Bind OS mechanisms without loading plugins or optional runtimes."""

from .platforms import install as _install_platform

_install_platform()
del _install_platform
