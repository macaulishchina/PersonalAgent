"""Optional Apple Home source adapter; sensor data never enters task/memory input."""

from assistant_runtime.api.system import system

from .declaration import DECLARATION

import asyncio
from dataclasses import asdict
import hmac
import json
import logging
import os
import secrets
import time

from aiohttp import web

from assistant_runtime.api.contracts import DeviceObservation, SensorReading
from .payload import UNITS, PayloadError, diagnostic_fields, parse_payload
from .settings import AppleHomeSettings, endpoint_settings

http_log = logging.getLogger(__name__ + ".http")


class AppleHomePlugin:
    manifest = DECLARATION.manifest

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
        self.refresh_state = {}
        self.context = None

    def maintenance_pending(self):
        return [{"id": name, "state": "running"} for name, task in self.refreshing.items() if not task.done()]

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
        system.private(token_path)
        self.token = token_path.read_text(encoding="utf-8").strip()
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
        refresh_status = self.refresh_state.get(ident, "idle")
        refresh_message = {
            "idle": "尚未请求刷新",
            "waiting": "正在等待家庭自动化回传",
            "received": "已收到回传，请按各项读数的有效性和采样时间使用",
            "timeout": (
                f"刷新等待 {self.settings.refresh_timeout} 秒超时，未收到新读数。"
                "已有值仅为上次读数，请注明采样时间；过期值不能当作当前值。"
                "后台继续接收，回传恢复后自动更新。"
            ),
            "unavailable": "刷新桥接未就绪或未配对，后台将继续尝试恢复",
        }[refresh_status]
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
                    "refresh_trigger": (
                        self.bridge.refresh_status(ident)
                        if self.bridge
                        else {"on": False, "subscriber_count": 0}
                    ),
                    "refresh": {
                        "status": refresh_status,
                        "timeout_seconds": self.settings.refresh_timeout,
                        "message": refresh_message,
                    },
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
        report_id = secrets.token_hex(6)
        data = None
        endpoints = {i: fields[0] for i, fields in self.endpoints.items()}
        try:
            data = await request.json()
            ident, readings = parse_payload(data, endpoints)
            # Reject reordered/replayed samples without renewing their freshness.
            timestamp = next(iter(readings.values())).observed_at
            old = max((r.observed_at or 0 for r in self.readings[ident].values()), default=0)
            if timestamp <= old:
                return web.json_response({"accepted": False, "reason": "duplicate_or_older_sample"})
            received = time.time()
            saved = {"received_at": received, "readings": {m: asdict(r) for m, r in readings.items()}}
            self.store.set("sensors.apple_home." + ident, saved)
            self.readings[ident], self.received[ident] = readings, received
            self.refresh_state[ident] = "received"
            self.observe(ident)
            self.changed[ident].set()
            self.context.publish("sensor.updated", device_id=ident, metrics=list(readings))
            return web.json_response({"accepted": True})
        except (TypeError, ValueError, KeyError, OverflowError, RecursionError) as exc:
            reason = str(exc) if isinstance(exc, PayloadError) else "Malformed sensor payload"
            field = exc.field if isinstance(exc, PayloadError) else "body"
            http_log.warning(
                "Rejected sensor report id=%s source=%s field=%s: %s",
                report_id,
                request.remote,
                field,
                reason,
            )
            if self.settings.log_invalid_values:
                details = diagnostic_fields(data, endpoints, self.token)
                if details:
                    http_log.warning(
                        "Sensor report values id=%s: %s", report_id, json.dumps(details, ensure_ascii=True)
                    )
            return web.json_response({"error": reason, "field": field, "report_id": report_id}, status=400)

    async def serve(self):
        from .bridge import RefreshBridge

        factory = self.bridge_factory or RefreshBridge
        while not self.stopping:
            try:
                app = web.Application(client_max_size=8192)
                app.router.add_post("/readings", self.receive)
                # Diagnose failed Apple-side setup without logging URLs, tokens
                # or complete sensor payloads. Invalid-value previews are opt-in.
                self.runner = web.AppRunner(
                    app, access_log=http_log, access_log_format="%a %s", shutdown_timeout=2
                )
                await self.runner.setup()
                await web.TCPSite(self.runner, self.settings.host, self.settings.port).start()
                self.receiver_ready = True
                self.bridge = factory(self.settings, self.root, {i: f[1] for i, f in self.endpoints.items()})
                await self.bridge.start()
                self.error = None
                self.context.set_health("ready")
                while not self.stopping:
                    await asyncio.sleep(5)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.error = "Apple Home source adapter unavailable: " + type(exc).__name__
                self.context.set_health("degraded", self.error)
            finally:
                await self.close_servers()
            if not self.stopping:
                await asyncio.sleep(10)

    async def refresh(self, ident):
        self.changed[ident].clear()
        if not self.bridge or not self.bridge.paired:
            self.refresh_state[ident] = "unavailable"
            self.observe(ident)
            return
        if time.monotonic() - self.last_pulse.get(ident, 0) < 5:
            return
        self.last_pulse[ident] = time.monotonic()
        if not self.bridge.pulse(ident):
            self.refresh_state[ident] = "unavailable"
            self.observe(ident)
            return
        self.refresh_state[ident] = "waiting"
        self.observe(ident)
        try:
            await asyncio.wait_for(self.changed[ident].wait(), self.settings.refresh_timeout)
        except TimeoutError:
            self.refresh_state[ident] = "timeout"
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
