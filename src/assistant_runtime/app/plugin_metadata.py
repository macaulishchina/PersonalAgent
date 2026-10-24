"""Installed platform declarations, read without importing extension code."""

from assistant_runtime.api.plugins import plugin_platforms


def installed_platforms(entry):
    module = entry.value.split(":")[0].replace(".", "/")
    resources = {module + "/platform.json", module + ".platform.json"}
    distribution = getattr(entry, "dist", None)
    matches = [file for file in distribution.files or () if str(file) in resources] if distribution else []
    if len(matches) != 1:
        raise ValueError(f"Plugin {entry.name}: missing or ambiguous static platform certification")
    return plugin_platforms(distribution.locate_file(matches[0]))
