"""Optional Apple Home source adapter; sensor data never enters task/memory input."""

import asyncio
from dataclasses import asdict
import hmac
import os
import secrets
import time

from aiohttp import web

from ...kernel.contracts import DeviceObservation, SensorReading
from ...kernel.runtime import Manifest
from .payload import UNITS, parse_payload
from .settings import AppleHomeSettings, endpoint_settings


class AppleHomePlugin:
    manifest = Manifest("apple_home")

    def __init__(self, config, store, directory, bridge_factory=None):
        self.config, self.store, self.directory = config, store, directory
        self.settings = AppleHomeSettings.parse(config.apple_home)
        self.endpoints = {
            ident: endpoint_settings(spec)
            for ident, spec in config.devices.items()
            if spec["adapter"] == "apple_home" and spec.get("enabled", True)
        }
        if not self.endpoints:
            raise ValueError("Apple Home plugin requires at least one configured sensor endpoint")
        self.bridge_factory = bridge_factory
        self.bridge = self.runner = None
        self.receiver_ready = self.stopping = False
        self.error = None
        self.readings, self.received, self.changed, self.refreshing = {}, {}, {}, {}
        self.last_pulse = {}
        self.context = None

    async def start(self, context):
        self.context = context
        self.root = self.config.state_dir / "apple-home"
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        token_path = self.root / "ingest-token.txt"
        if token_path.is_symlink():
            raise ValueError("Sensor token cannot be a symlink")
        if not token_path.exists():
            fd = os.open(token_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(secrets.token_urlsafe(32) + "\n")
        token_path.chmod(0o600)
        self.token = token_path.read_text().strip()
        if len(self.token) < 24:
            raise ValueError("Sensor ingest token must have at least 24 characters")
        for ident, (metrics, _) in self.endpoints.items():
            self.changed[ident] = asyncio.Event()
            self.readings[ident] = {
                m: SensorReading(m, None, UNITS[m], "Apple Home", status="unavailable") for m in metrics
            }
            saved = self.store.get("sensors.apple_home." + ident)
            if saved:
                try:
                    loaded = {m: SensorReading(**r) for m, r in saved["readings"].items()}
                    if set(loaded) == set(metrics):
                        self.readings[ident] = loaded
                        self.received[ident] = saved["received_at"]
                except (TypeError, ValueError, KeyError):
                    self.error = "Saved sensor state could not be restored"

            async def probe(ident=ident):
                await self.probe(ident)

            self.directory.register("apple_home", ident, tuple("sensor." + m for m in metrics), probe)
            self.observe(ident)
        context.own(self.close)
        context.spawn(self.serve(), "bridge-services")
        context.spawn(self.poll(), "sensor-refresh")

    def observe(self, ident):
        now = time.time()
        limit = self.config.devices[ident].get("max_age_seconds", 180)
        current = any(
            r.status == "valid" and r.observed_at is not None and 0 <= now - r.observed_at <= limit
            for r in self.readings[ident].values()
        )
        paired = bool(self.bridge and self.bridge.paired)
        error = (
            None
            if current
            else (
                self.error
                or (
                    "等待 Apple 家庭配对和温湿度自动化回传"
                    if not paired
                    else "尚未收到有效读数或读数已过期，请检查家庭自动化"
                )
            )
        )
        self.directory.observe(
            "apple_home",
            ident,
            DeviceObservation(
                "available" if current else "unavailable",
                {
                    "source": "Apple Home automation",
                    "receiver_ready": self.receiver_ready,
                    "refresh_bridge_paired": paired,
                    "trigger_name": self.endpoints[ident][1],
                    "last_received_at": self.received.get(ident),
                    "reading_time_basis": "source_observed_at",
                },
                error=error,
                readings=self.readings[ident],
            ),
        )

    async def receive(self, request):
        if self.stopping:
            raise web.HTTPServiceUnavailable(text="Sensor adapter is stopping")
        expected = "Bearer " + self.token
        if not hmac.compare_digest(request.headers.get("Authorization", "").encode(), expected.encode()):
            raise web.HTTPUnauthorized(text="Sensor source authorization required")
        try:
            data = await request.json()
            ident, readings = parse_payload(data, {i: fields[0] for i, fields in self.endpoints.items()})
            # Reject reordered/replayed samples without renewing their freshness.
            timestamp = next(iter(readings.values())).observed_at
            old = max((r.observed_at or 0 for r in self.readings[ident].values()), default=0)
            if timestamp <= old:
                return web.json_response({"accepted": False, "reason": "duplicate_or_older_sample"})
            received = time.time()
            saved = {"received_at": received, "readings": {m: asdict(r) for m, r in readings.items()}}
            self.store.set("sensors.apple_home." + ident, saved)
            self.readings[ident], self.received[ident] = readings, received
            self.observe(ident)
            self.changed[ident].set()
            self.context.publish("sensor.updated", device_id=ident, metrics=list(readings))
            return web.json_response({"accepted": True})
        except (TypeError, ValueError, KeyError) as exc:
            return web.json_response({"error": str(exc)[:180]}, status=400)

    async def serve(self):
        from .bridge import RefreshBridge

        factory = self.bridge_factory or RefreshBridge
        while not self.stopping:
            try:
                app = web.Application(client_max_size=8192)
                app.router.add_post("/readings", self.receive)
                self.runner = web.AppRunner(app, access_log=None, shutdown_timeout=2)
                await self.runner.setup()
                await web.TCPSite(self.runner, self.settings.host, self.settings.port).start()
                self.receiver_ready = True
                self.bridge = factory(self.settings, self.root, {i: f[1] for i, f in self.endpoints.items()})
                await self.bridge.start()
                self.error = self.context.error = None
                self.context.state = "ready"
                while not self.stopping:
                    await asyncio.sleep(5)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.error = "Apple Home source adapter unavailable: " + type(exc).__name__
                self.context.error, self.context.state = self.error, "degraded"
            finally:
                await self.close_servers()
            if not self.stopping:
                await asyncio.sleep(10)

    async def refresh(self, ident):
        self.changed[ident].clear()
        if not self.bridge or not self.bridge.paired:
            return
        if time.monotonic() - self.last_pulse.get(ident, 0) < 5:
            return
        self.last_pulse[ident] = time.monotonic()
        if not self.bridge.pulse(ident):
            return
        try:
            await asyncio.wait_for(self.changed[ident].wait(), self.settings.refresh_timeout)
        except TimeoutError:
            pass
        finally:
            self.observe(ident)

    async def probe(self, ident):
        task = self.refreshing.get(ident)
        if task is None or task.done():
            task = self.context.spawn(self.refresh(ident), "sensor-read-" + ident)
            self.refreshing[ident] = task
        await asyncio.shield(task)
        self.observe(ident)

    async def poll(self):
        deadlines = {ident: 0.0 for ident in self.endpoints}
        while True:
            for ident in self.endpoints:
                self.observe(ident)
                if time.monotonic() >= deadlines[ident]:
                    deadlines[ident] = time.monotonic() + self.settings.poll_seconds
                    task = self.refreshing.get(ident)
                    if task is None or task.done():
                        self.refreshing[ident] = self.context.spawn(
                            self.refresh(ident), "sensor-poll-" + ident
                        )
            await asyncio.sleep(1)

    async def close_servers(self):
        self.receiver_ready = False
        bridge, self.bridge = self.bridge, None
        runner, self.runner = self.runner, None
        try:
            if bridge:
                try:
                    await bridge.close()
                except Exception as exc:
                    self.error = "Apple Home bridge cleanup failed: " + type(exc).__name__
        finally:
            if runner:
                await runner.cleanup()

    async def close(self):
        self.stopping = True
        await self.close_servers()
