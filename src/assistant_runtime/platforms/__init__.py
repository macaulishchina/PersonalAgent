"""Bundled OS mechanisms; optional libraries load only when the mechanism is used."""


def install():
    from assistant_runtime.api.system import bind_system
    from .common import Platform

    bind_system(Platform())
