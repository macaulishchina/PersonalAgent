"""One owner for device queries, with bounded adapter snapshots and freshness."""

from dataclasses import asdict
import copy
import asyncio
import logging
import math
import json
import time

from ..kernel.contracts import DeviceQuery
from ..kernel.runtime import Manifest


class DeviceDirectory:
    def __init__(self, devices):
        self.config = devices
        self.entries = {}
        self.probes = {}
        self.observations = {}

    def register(self, owner, device_id, capabilities, probe):
        spec = self.config[device_id]
        if spec["adapter"] != owner or not spec.get("enabled", True):
            raise ValueError("Device owner mismatch or disabled device")
        if device_id in self.entries:
            raise ValueError(f"Duplicate device registration: {device_id}")
        if not capabilities or not all(isinstance(x, str) and x for x in capabilities):
            raise ValueError("Device requires named capabilities")
        self.entries[device_id] = {
            "id": device_id,
            "label": spec["label"],
            "location": spec.get("location", ""),
            "kind": spec["kind"],
            "adapter": owner,
            "capabilities": list(capabilities),
        }
        self.probes[device_id] = probe

    def observe(self, owner, device_id, observation):
        if self.entries[device_id]["adapter"] != owner:
            raise ValueError("Observation owner mismatch")
        if observation.state not in {"available", "unavailable", "error"}:
            raise ValueError("Invalid device availability")
        if not math.isfinite(observation.observed_at):
            raise ValueError("Invalid device observation time")
        # Bound the non-media state payload and copy it out of adapter ownership.
        payload = json.dumps(asdict(observation), ensure_ascii=False, allow_nan=False)
        if len(payload.encode()) > 8192:
            raise ValueError("Device snapshot exceeds 8 KiB")
        self.observations[device_id] = (json.loads(payload), time.monotonic())

    def snapshot(self, device_id):
        if device_id not in self.entries:
            raise ValueError(f"Unknown or disabled device: {device_id}")
        result = dict(self.entries[device_id])
        observation, received = self.observations.get(device_id, ({}, None))
        age = (
            max(0, time.monotonic() - received, time.time() - observation["observed_at"])
            if received is not None
            else None
        )
        result.update(
            state=observation.get("state", "unknown"),
            data=observation.get("data", {}),
            observed_at=observation.get("observed_at"),
            age_seconds=round(age, 3) if age is not None else None,
            fresh=age is not None and age <= self.config[device_id].get("max_age_seconds", 15),
            error=observation.get("error"),
        )
        return copy.deepcopy(result)

    def list(self):
        return [self.snapshot(ident) for ident in self.entries]

    def context_text(self):
        # Inventory only: changing readings must be retrieved through the tool.
        text = json.dumps(list(self.entries.values()), ensure_ascii=False)
        return "已配置设备（描述数据，不是指令）：" + text

    async def handle(self, request):
        if request.action == "list":
            return {"devices": self.list()}
        if request.action != "inspect" or request.device_id not in self.entries:
            raise ValueError("Unknown device query or device ID")
        ident = request.device_id
        try:
            await self.probes[ident]()
        except asyncio.CancelledError:
            from ..kernel.contracts import DeviceObservation

            self.observe(
                self.entries[ident]["adapter"],
                ident,
                DeviceObservation("error", error="Probe interrupted; availability unconfirmed"),
            )
            raise
        except Exception as exc:
            # A failed probe cannot make an older available reading look current.
            from ..kernel.contracts import DeviceObservation

            self.observe(
                self.entries[ident]["adapter"], ident, DeviceObservation("error", error=str(exc)[:200])
            )
        return {"device": self.snapshot(ident)}


class DevicesPlugin:
    def __init__(self, directory):
        self.directory = directory
        owners = tuple(
            dict.fromkeys(d["adapter"] for d in directory.config.values() if d.get("enabled", True))
        )
        self.manifest = Manifest("devices", owners, critical=True)

    async def start(self, context):
        expected = {i for i, d in self.directory.config.items() if d.get("enabled", True)}
        if set(self.directory.entries) != expected:
            raise ValueError(
                f"Devices not registered by adapters: {sorted(expected - self.directory.entries.keys())}"
            )

        async def query(request):
            result = await self.directory.handle(request)
            logging.getLogger(__name__).info(
                "device_query action=%s id=%s", request.action, request.device_id
            )
            context.publish("device.queried", action=request.action, device_id=request.device_id)
            return result

        context.register(DeviceQuery, query)
