"""Platform defaults for new or implicit instances; explicit selections are never filtered."""

from pathlib import Path
import tomllib
from assistant_runtime.api.system import system
from assistant_runtime.api.configuration import configuration_default


def default_preset():
    return "slim" if system.name == "windows" else "standard"


def default_plugins():
    if system.name == "windows":
        template = Path(__file__).with_name("templates") / "slim/config.toml"
        return tomllib.loads(template.read_text(encoding="utf-8"))["plugins"]
    return configuration_default(__file__, "plugins")
