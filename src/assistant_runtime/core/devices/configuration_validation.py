"""Device semantics are validated only when the device capability is enabled."""

import re


def validate_devices(self):
    if not isinstance(self.devices, dict) or len(self.devices) > 32:
        raise ValueError("devices must be a table with at most 32 entries")
    for ident, spec in self.devices.items():
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", ident) or not isinstance(spec, dict):
            raise ValueError("Invalid device ID or definition")
        allowed = {
            "adapter",
            "kind",
            "label",
            "location",
            "connection",
            "enabled",
            "max_age_seconds",
            "physical_id",
        }
        if set(spec) - allowed:
            raise ValueError(f"Unknown device settings: {ident}")
        for name in ("adapter", "kind", "label"):
            if not isinstance(spec.get(name), str) or not spec[name].strip() or len(spec[name]) > 120:
                raise ValueError(f"Invalid device {name}: {ident}")
        physical = spec.get("physical_id", ident)
        if not isinstance(physical, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", physical):
            raise ValueError("Invalid physical device association")
        if not isinstance(spec.get("connection"), dict):
            raise ValueError(f"Device connection must be a table: {ident}")
        if not isinstance(spec.get("location", ""), str) or len(spec.get("location", "")) > 120:
            raise ValueError("Invalid device location")
        if type(spec.get("enabled", True)) is not bool:
            raise ValueError("Device enabled must be boolean")
        age = spec.get("max_age_seconds", 15)
        if type(age) not in (int, float) or not 0 < age <= 3600:
            raise ValueError("Invalid device freshness period")
        if (
            "devices" in self.enabled_plugins
            and spec.get("enabled", True)
            and spec["adapter"] not in self.plugins
        ):
            raise ValueError(f"Enabled device adapter not in plugins: {ident}")
