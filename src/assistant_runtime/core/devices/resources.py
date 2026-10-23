"""Explicit device resource sharing. Leases never stop or reroute another owner."""

import uuid


class DeviceBusy(RuntimeError):
    pass


class ResourceLease:
    def __init__(self, resources, key, ident):
        self.resources, self.key, self.ident = resources, key, ident

    def release(self):
        holders = self.resources.held.get(self.key, {})
        holders.pop(self.ident, None)
        if not holders:
            self.resources.held.pop(self.key, None)


class DeviceResources:
    def __init__(self):
        self.held = {}

    def acquire(self, device_id, resource, owner, *, shared=False):
        if (
            any(not isinstance(v, str) or not v for v in (device_id, resource, owner))
            or type(shared) is not bool
        ):
            raise ValueError("A device resource requires explicit identity, owner and sharing policy")
        key = (device_id, resource)
        holders = self.held.get(key, {})
        if holders and (not shared or any(not h["shared"] for h in holders.values())):
            raise DeviceBusy(f"Device resource busy: {device_id}/{resource}; existing operation preserved")
        ident = uuid.uuid4().hex
        self.held.setdefault(key, {})[ident] = {"owner": owner, "shared": shared}
        return ResourceLease(self, key, ident)

    def snapshot(self, device_id):
        return [
            {"resource": resource, "owners": [dict(v) for v in holders.values()]}
            for (ident, resource), holders in self.held.items()
            if ident == device_id
        ]
