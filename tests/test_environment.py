"""Sensor contracts, Apple Home source failures and real upstream bridge lifecycle."""

import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import socket
import tempfile
from types import SimpleNamespace
import time
import unittest
from unittest.mock import AsyncMock, patch

from aiohttp import ClientSession
from fastmcp import Client
from pyhap.accessory_driver import get_topic

from assistant_runtime.app.config import Config
from assistant_runtime.core.devices.directory import DeviceDirectory, DevicesPlugin
from assistant_runtime.api.contracts import DeviceObservation, SensorReading
from assistant_runtime.kernel.runtime import Runtime
from assistant_runtime.core.devices.tools import create_server
from assistant_runtime.plugins.apple_home.bridge import RefreshBridge
from assistant_runtime.plugins.apple_home.payload import normalize, parse_payload, parse_time
from assistant_runtime.plugins.apple_home.plugin import AppleHomePlugin
from assistant_runtime.plugins.apple_home.settings import AppleHomeSettings, endpoint_settings
from assistant_runtime.app.store import Store


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def spec():
    return {
        "adapter": "apple_home",
        "kind": "sensor",
        "label": "测试温湿度",
        "location": "测试",
        "physical_id": "test_homepod",
        "max_age_seconds": 180,
        "connection": {"metrics": ["temperature", "humidity"], "trigger_name": "读取测试温湿度"},
    }


class ReadingTest(unittest.TestCase):
    def test_contract_rejects_ambiguous_invalid_values(self):
        base = dict(metric="temperature", value=24.5, unit="°C", source="fixture", observed_at=100)
        for changes in (
            {"metric": []},
            {"source": 5},
            {"unit": None},
            {"value": True},
            {"value": float("nan")},
            {"observed_at": True},
            {"observed_at": "100"},
            {"status": "calibrating"},
            {"status": []},
            {"observed_at": None},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                SensorReading(**(base | changes))
        self.assertIsNone(SensorReading("temperature", None, "°C", "fixture", status="calibrating").value)

    def test_normalizes_measurements_without_mixing_validity(self):
        now = time.time()
        self.assertEqual(normalize("temperature", "75.2 °F", now).value, 24)
        self.assertEqual(normalize("temperature", "24,5 摄氏度", now).value, 24.5)
        self.assertEqual(normalize("humidity", "46 %", now).value, 46)
        self.assertEqual(normalize("humidity", None, now).status, "unavailable")
        self.assertEqual(normalize("temperature", {"status": "calibrating"}, now).status, "calibrating")
        for metric, raw in (
            ("humidity", 101),
            ("humidity", -1),
            ("humidity", True),
            ("temperature", "invalid"),
            ("temperature", float("inf")),
            ("temperature", {"value": 24, "unit": []}),
            ("temperature", {"value": 24, "unit": "K"}),
            ("temperature", {"value": 24, "status": "calibrating"}),
            ("temperature", {"value": 24, "execute": "command"}),
        ):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                normalize(metric, raw, now)

    def test_payload_is_atomic_explicit_and_time_zoned(self):
        sample = {
            "device_id": "environment",
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "temperature": 24,
            "humidity": 50,
        }
        expected = {"environment": ("temperature", "humidity")}
        self.assertEqual(set(parse_payload(sample, expected)[1]), {"temperature", "humidity"})
        for changes in (
            {"device_id": "other"},
            {"observed_at": "2026-10-14T01:00:00"},
            {"observed_at": time.time() + 60},
            {"observed_at": True},
            {"observed_at": float("nan")},
            {"humidity": 200},
            {"command": "wake"},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                parse_payload(sample | changes, expected)
        with self.assertRaises(ValueError):
            parse_payload({k: v for k, v in sample.items() if k != "humidity"}, expected)
        self.assertAlmostEqual(parse_time(sample["observed_at"]), time.time(), delta=1)

    def test_strict_source_settings(self):
        self.assertEqual(AppleHomeSettings.parse({}).refresh_timeout, 10)
        self.assertEqual(AppleHomeSettings.parse({"refresh_timeout": 10}).refresh_timeout, 10)
        for settings in (
            {"host": "0.0.0.0"},
            {"port": True},
            {"poll_seconds": 1},
            {"refresh_timeout": 11},
            {"refresh_timeout": 0},
            {"refresh_timeout": True},
            {"log_invalid_values": "yes"},
            {"port": 8047, "hap_port": 8047},
            {"unexpected": 1},
        ):
            with self.assertRaises(ValueError):
                AppleHomeSettings.parse(settings)
        for metrics in ([{}], ["temperature", "temperature"], ["pressure"], []):
            with self.assertRaises(ValueError):
                endpoint_settings(spec() | {"connection": {"metrics": metrics}})
        for physical in ("Invalid name", 1, ""):
            with self.assertRaises(ValueError):
                Config(plugins=["apple_home"], devices={"environment": spec() | {"physical_id": physical}})


class SensorDirectoryTest(unittest.TestCase):
    def setUp(self):
        self.directory = DeviceDirectory(
            {
                "environment": spec(),
                "speaker": {
                    "adapter": "airplay",
                    "kind": "speaker",
                    "label": "测试播放",
                    "physical_id": "test_homepod",
                    "connection": {},
                },
            }
        )
        self.directory.register(
            "apple_home", "environment", ["sensor.temperature", "sensor.humidity"], AsyncMock()
        )
        self.directory.register("airplay", "speaker", ["audio.playback"], AsyncMock())

    def test_independent_metric_clocks_do_not_reset_on_heartbeats(self):
        with patch("time.time", return_value=1000), patch("time.monotonic", return_value=10):
            readings = {
                "temperature": SensorReading("temperature", 24, "°C", "fixture", 990),
                "humidity": SensorReading("humidity", 50, "%", "fixture", 800),
            }
            self.directory.observe(
                "apple_home",
                "environment",
                DeviceObservation("available", observed_at=1000, readings=readings),
            )
        with patch("time.time", return_value=1100), patch("time.monotonic", return_value=110):
            self.directory.observe(
                "apple_home",
                "environment",
                DeviceObservation("available", observed_at=1100, readings=readings),
            )
            snap = self.directory.snapshot("environment")
            self.assertTrue(snap["fresh"])
            self.assertTrue(snap["readings"]["temperature"]["fresh"])
            self.assertEqual(snap["readings"]["humidity"]["status"], "stale")
            self.assertEqual(snap["readings"]["humidity"]["age_seconds"], 300)
        # A wall-clock correction cannot rejuvenate samples already aged in-process.
        with patch("time.time", return_value=900), patch("time.monotonic", return_value=210):
            self.directory.observe(
                "apple_home",
                "environment",
                DeviceObservation("available", observed_at=900, readings=readings),
            )
            self.assertEqual(
                self.directory.snapshot("environment")["readings"]["temperature"]["status"], "stale"
            )

    def test_linked_endpoints_have_independent_owners_and_readings(self):
        readings = {
            "temperature": SensorReading("temperature", 24, "°C", "fixture", time.time()),
            "humidity": SensorReading("humidity", None, "%", "fixture", status="calibrating"),
        }
        self.directory.observe("airplay", "speaker", DeviceObservation("available"))
        self.directory.observe("apple_home", "environment", DeviceObservation("available", readings=readings))
        speaker, sensor = self.directory.snapshot("speaker"), self.directory.snapshot("environment")
        self.assertEqual(speaker["physical_id"], sensor["physical_id"])
        self.assertTrue(sensor["readings"]["temperature"]["fresh"])
        self.assertEqual(sensor["readings"]["humidity"]["status"], "calibrating")
        self.directory.observe(
            "apple_home", "environment", DeviceObservation("unavailable", readings=readings)
        )
        self.assertFalse(self.directory.snapshot("environment")["readings"]["temperature"]["fresh"])
        self.assertEqual(self.directory.snapshot("speaker")["state"], "available")
        with self.assertRaises(ValueError):
            self.directory.observe("airplay", "speaker", DeviceObservation("available", readings=readings))


class FakeBridge:
    def __init__(self, *args):
        self.paired = False
        self.pulses = 0
        self.closed = False

    async def start(self):
        pass

    def pulse(self, ident):
        self.pulses += 1
        return True

    def refresh_status(self, ident):
        return {"on": False, "subscriber_count": 0}

    async def close(self):
        self.closed = True


class AppleSourceTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        with socket.socket() as receiver, socket.socket() as bridge:
            receiver.bind(("127.0.0.1", 0))
            bridge.bind(("127.0.0.1", 0))
            receiver_port, bridge_port = receiver.getsockname()[1], bridge.getsockname()[1]
        self.config = Config(
            root=Path(self.temp.name),
            plugins=["apple_home"],
            devices={"environment": spec()},
            apple_home={"port": receiver_port, "hap_port": bridge_port, "refresh_timeout": 1},
        )
        self.store = Store(self.config.state_dir / "assistant.db")
        self.client = ClientSession()
        await self.launch()

    async def launch(self, factory=FakeBridge):
        self.directory = DeviceDirectory(self.config.devices)
        self.plugin = AppleHomePlugin(self.config, self.store, self.directory, factory)
        self.runtime = Runtime(self.store)
        self.runtime.add(self.plugin)
        self.runtime.add(DevicesPlugin(self.directory))
        self.events = self.runtime.subscribe("test")
        await self.runtime.start()
        async with asyncio.timeout(3):
            while not self.plugin.receiver_ready or self.plugin.bridge is None:
                await asyncio.sleep(0.01)
        self.url = f"http://127.0.0.1:{self.config.apple_home['port']}/readings"
        self.headers = {"Authorization": "Bearer " + self.plugin.token}

    async def asyncTearDown(self):
        await self.client.close()
        await self.runtime.close()
        self.store.db.close()
        self.temp.cleanup()

    async def post(self, sample, headers=None):
        async with self.client.post(
            self.url, json=sample, headers=self.headers if headers is None else headers
        ) as response:
            return response.status, await response.text()

    def sample(self, timestamp=None):
        return {
            "device_id": "environment",
            "observed_at": timestamp or time.time() - 1,
            "temperature": "24 °C",
            "humidity": "46 %",
        }

    async def test_authenticated_atomic_ingest_replay_and_no_implicit_actions(self):
        sample = self.sample()
        self.assertEqual((await self.post(sample, {}))[0], 401)
        self.assertEqual((await self.post(sample, {"Authorization": "非凭据"}))[0], 401)
        self.assertEqual((await self.post(sample))[0], 200)
        saved = self.store.get("sensors.apple_home.environment")
        self.assertIn('"accepted": false', (await self.post(sample))[1])
        self.assertIn('"accepted": false', (await self.post(self.sample(sample["observed_at"] - 10)))[1])
        self.assertEqual((await self.post(self.sample() | {"humidity": 300}))[0], 400)
        self.assertEqual(self.store.get("sensors.apple_home.environment"), saved)
        async with self.client.post(self.url, data="x" * 9000, headers=self.headers) as response:
            self.assertEqual(response.status, 413)
        # Sensor ingestion must not initialize unrelated conversation/execution/memory owners.
        tables = {
            row[0] for row in self.store.db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        self.assertFalse(tables & {"transcripts", "tasks", "memory_jobs"})
        events = []
        while not self.events.empty():
            events.append(self.events.get_nowait().kind)
        self.assertEqual(events, ["sensor.updated"])
        self.assertEqual((self.plugin.root / "ingest-token.txt").stat().st_mode & 0o777, 0o600)

    async def test_http_diagnostics_include_status_without_credentials_or_payload(self):
        with self.assertLogs("assistant_runtime.plugins.apple_home.plugin.http", level="INFO") as logs:
            async with self.client.post(
                self.url + "?secret=private-query", json={"secret": "private-body"}, headers=self.headers
            ) as response:
                self.assertEqual(response.status, 400)
                await response.read()
            self.assertEqual((await self.post(self.sample(), {"Authorization": "private-header"}))[0], 401)
            self.assertEqual((await self.post(self.sample() | {"observed_at": self.plugin.token}))[0], 400)
            await asyncio.sleep(0)
        messages = [record.getMessage() for record in logs.records]
        self.assertEqual(
            [m for m in messages if m.startswith("127.0.0.1")],
            ["127.0.0.1 400", "127.0.0.1 401", "127.0.0.1 400"],
        )
        warnings = [m for m in messages if m.startswith("Rejected sensor report")]
        self.assertEqual(len(warnings), 2)
        self.assertIn("field=device_id: Sensor payload requires device_id", warnings[0])
        self.assertIn("field=observed_at: Sensor timestamp must be ISO 8601 time with timezone", warnings[1])
        for secret in ("private-query", "private-body", "private-header"):
            self.assertNotIn(secret, "\n".join(messages))
        self.assertNotIn(self.plugin.token, "\n".join(messages))

    async def test_invalid_value_diagnostics_identify_field_type_and_bounded_original(self):
        self.plugin.settings = AppleHomeSettings.parse({**self.config.apple_home, "log_invalid_values": True})
        cases = (
            ("temperature", "24 °C\n25 °C"),
            ("humidity", ["46 %", "47 %"]),
            ("temperature", "x" * 2000 + "unlogged-tail"),
            ("temperature", 10**400),
        )
        for field, raw in cases:
            with self.subTest(field=field, raw_type=type(raw).__name__):
                with self.assertLogs(
                    "assistant_runtime.plugins.apple_home.plugin.http", level="WARNING"
                ) as logs:
                    status, body = await self.post(self.sample() | {field: raw})
                response = json.loads(body)
                self.assertEqual(status, 400)
                self.assertEqual(response["field"], field)
                self.assertTrue(all(response["report_id"] in r.getMessage() for r in logs.records))
                details = json.loads(logs.records[1].getMessage().split(": ", 1)[1])
                self.assertEqual(details["device_id"], "environment")
                self.assertEqual(details["fields"][field]["type"], type(raw).__name__)
                self.assertEqual(set(details["fields"]), {"observed_at", "temperature", "humidity"})
                self.assertLessEqual(len(details["fields"][field]["preview"]), 256)
                self.assertFalse(any("\n" in r.getMessage() for r in logs.records))
                if isinstance(raw, str) and len(raw) > 256:
                    self.assertTrue(details["fields"][field]["truncated"])
                    self.assertNotIn("unlogged-tail", logs.records[1].getMessage())
        self.assertIsNone(self.store.get("sensors.apple_home.environment"))

    async def test_value_diagnostics_exclude_unauthorized_unknown_and_unrelated_data(self):
        self.plugin.settings = AppleHomeSettings.parse({**self.config.apple_home, "log_invalid_values": True})
        with self.assertLogs("assistant_runtime.plugins.apple_home.plugin.http", level="INFO") as logs:
            self.assertEqual(
                (await self.post(self.sample() | {"temperature": "unauthorized-value"}, {}))[0], 401
            )
            self.assertEqual(
                (await self.post(self.sample() | {"device_id": "unknown", "temperature": "unknown-value"}))[
                    0
                ],
                400,
            )
            self.assertEqual(
                (await self.post(self.sample() | {"humidity": self.plugin.token, "secret": "private-body"}))[
                    0
                ],
                400,
            )
        messages = "\n".join(r.getMessage() for r in logs.records)
        for secret in ("unauthorized-value", "unknown-value", "private-body", self.plugin.token):
            self.assertNotIn(secret, messages)
        self.assertIn("[redacted]", messages)
        self.assertEqual(messages.count("Sensor report values id="), 1)

    async def test_metric_validity_mcp_readback_and_restart_never_rejuvenates(self):
        sample = self.sample() | {"humidity": {"status": "calibrating"}}
        self.assertEqual((await self.post(sample))[0], 200)
        handler = SimpleNamespace(invoke=self.directory.handle, status=AsyncMock())
        async with Client(create_server(handler)) as mcp:
            result = (await mcp.call_tool("inspect_device", {"device_id": "environment"})).structured_content[
                "device"
            ]
            self.assertEqual(result["readings"]["temperature"]["value"], 24)
            self.assertTrue(result["readings"]["temperature"]["fresh"])
            self.assertEqual(result["readings"]["humidity"]["status"], "calibrating")
        saved = self.store.get("sensors.apple_home.environment")
        for reading in saved["readings"].values():
            reading["observed_at"] = time.time() - 360
        self.store.set("sensors.apple_home.environment", saved)
        await self.runtime.close()
        await self.launch()
        snapshot = self.directory.snapshot("environment")
        self.assertEqual(snapshot["readings"]["temperature"]["status"], "stale")
        self.assertEqual(
            snapshot["readings"]["temperature"]["observed_at"],
            saved["readings"]["temperature"]["observed_at"],
        )
        self.assertEqual(snapshot["state"], "unavailable")

    async def test_concurrent_queries_coalesce_refresh_and_preserve_capture_owner(self):
        self.plugin.bridge.paired = True
        first = asyncio.create_task(self.plugin.probe("environment"))
        second = asyncio.create_task(self.plugin.probe("environment"))
        await asyncio.sleep(0.03)
        self.assertEqual(self.plugin.bridge.pulses, 1)
        await self.post(self.sample())
        await asyncio.wait_for(asyncio.gather(first, second), 1)
        # No new reading: timeout returns real state, not a fabricated refresh.
        self.plugin.last_pulse.clear()
        started = time.monotonic()
        await self.plugin.probe("environment")
        self.assertLess(time.monotonic() - started, 1.5)
        self.assertEqual(self.plugin.bridge.pulses, 2)

    async def test_timeout_keeps_receiver_alive_and_late_sample_recovers_via_mcp(self):
        self.plugin.bridge.paired = True
        handler = SimpleNamespace(invoke=self.directory.handle, status=AsyncMock())
        async with Client(create_server(handler)) as mcp:
            started = time.monotonic()
            result = (await mcp.call_tool("inspect_device", {"device_id": "environment"})).structured_content[
                "device"
            ]
            elapsed = time.monotonic() - started
            self.assertGreaterEqual(elapsed, 0.9)
            self.assertLess(elapsed, 2)
            self.assertEqual(result["data"]["refresh"]["status"], "timeout")
            self.assertEqual(result["state"], "unavailable")
            self.assertIsNone(result["readings"]["temperature"]["value"])
            self.assertTrue(self.plugin.receiver_ready)
            self.assertFalse(self.runtime.fatal.is_set())
            # The same receiver accepts a late real-format sample without restart.
            sample = self.sample()
            self.assertEqual((await self.post(sample))[0], 200)
            devices = (await mcp.call_tool("list_devices", {})).structured_content["devices"]
            result = next(d for d in devices if d["id"] == "environment")
            self.assertEqual(result["data"]["refresh"]["status"], "received")
            self.assertEqual(result["state"], "available")
            self.assertTrue(result["readings"]["temperature"]["fresh"])
            self.assertEqual(result["readings"]["temperature"]["observed_at"], sample["observed_at"])
            self.assertEqual(self.plugin.bridge.pulses, 1)

    async def test_timeout_preserves_stale_sample_and_replay_cannot_clear_it(self):
        self.plugin.bridge.paired = True
        old = self.sample(time.time() - 360)
        self.assertEqual((await self.post(old))[0], 200)
        received = self.plugin.received["environment"]
        await self.plugin.probe("environment")
        self.assertIn('"accepted": false', (await self.post(old))[1])
        result = self.directory.snapshot("environment")
        self.assertEqual(result["data"]["refresh"]["status"], "timeout")
        self.assertEqual(result["data"]["last_received_at"], received)
        self.assertEqual(result["readings"]["temperature"]["status"], "stale")
        self.assertEqual(result["readings"]["temperature"]["observed_at"], old["observed_at"])
        self.assertFalse(result["readings"]["temperature"]["fresh"])
        # A new report can still say that a sensor is calibrating, not usable.
        await self.post(self.sample() | {"humidity": {"status": "calibrating"}})
        result = self.directory.snapshot("environment")
        self.assertEqual(result["data"]["refresh"]["status"], "received")
        self.assertEqual(result["readings"]["humidity"]["status"], "calibrating")
        self.assertFalse(result["readings"]["humidity"]["fresh"])
        self.assertTrue(result["readings"]["temperature"]["fresh"])

    async def test_optional_bridge_failure_keeps_directory_alive_and_releases_receiver(self):
        await self.runtime.close()

        class BrokenBridge(FakeBridge):
            async def start(self):
                raise OSError("unavailable source")

        self.directory = DeviceDirectory(self.config.devices)
        self.plugin = AppleHomePlugin(self.config, self.store, self.directory, BrokenBridge)
        self.runtime = Runtime(self.store)
        self.runtime.add(self.plugin)
        self.runtime.add(DevicesPlugin(self.directory))
        await self.runtime.start()
        async with asyncio.timeout(2):
            while self.runtime.contexts["apple_home"].state != "degraded":
                await asyncio.sleep(0.01)
        self.assertEqual(self.runtime.contexts["devices"].state, "ready")
        self.assertFalse(self.runtime.fatal.is_set())
        self.assertEqual(self.directory.snapshot("environment")["state"], "unavailable")
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", self.config.apple_home["port"]))


class UpstreamBridgeTest(unittest.IsolatedAsyncioTestCase):
    async def test_real_hap_start_close_persistence_and_stable_accessory_ids(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            settings = AppleHomeSettings(hap_port=free_port())
            bridge = RefreshBridge(settings, root, {"second": "第二个读数触发器"})
            await bridge.start()
            try:
                reader, writer = await asyncio.open_connection(settings.host, settings.hap_port)
                writer.write(b"GET /accessories HTTP/1.1\r\nHost: localhost\r\n\r\n")
                await writer.drain()
                self.assertIn(b"401 Unauthorized", await asyncio.wait_for(reader.read(4096), 2))
                writer.close()
                await writer.wait_closed()
                aid = bridge.lamps["second"].aid
                mac = bridge.driver.state.mac
                pincode = bridge.driver.state.pincode
                self.assertFalse(bridge.pulse("second"))
            finally:
                await bridge.close()
            self.assertEqual((root / "pairing-code.txt").stat().st_mode & 0o777, 0o600)
            with socket.socket() as sock:
                sock.bind((settings.host, settings.hap_port))
            bridge = RefreshBridge(
                settings, root, {"first": "第一个读数触发器", "second": "第二个读数触发器"}
            )
            await bridge.start()
            try:
                self.assertEqual(bridge.lamps["second"].aid, aid)
                self.assertEqual(bridge.driver.state.mac, mac)
                self.assertEqual(bridge.driver.state.pincode, pincode)
                self.assertEqual((root / "pairing-code.txt").read_text().strip().encode(), pincode)
                self.assertNotEqual(bridge.lamps["first"].aid, aid)
                bridge.driver.state.paired_clients["fixture"] = b"fixture"
                lamp = bridge.lamps["second"]
                self.assertFalse(bridge.refresh_status("second")["on"])
                self.assertEqual(bridge.refresh_status("second")["subscriber_count"], 0)
                self.assertEqual(bridge.refresh_status("second")["controller_write_count"], 0)
                topic = get_topic(lamp.aid, lamp.iid_manager.get_iid(lamp.on))
                bridge.driver.async_subscribe_client_topic(("127.0.0.1", 12345), topic, True)
                with patch.object(bridge.driver.http_server, "push_event", return_value=True) as notify:
                    self.assertTrue(bridge.pulse("second"))
                    self.assertTrue(bridge.refresh_status("second")["on"])
                    self.assertEqual(bridge.refresh_status("second")["subscriber_count"], 1)
                    await asyncio.sleep(1.1)
                    self.assertFalse(lamp.on.value)
                    self.assertEqual([c.args[0]["value"] for c in notify.call_args_list], [True, False])
                    self.assertEqual(bridge.refresh_status("second")["controller_write_count"], 0)
                    before_write = time.time()
                    lamp.on.client_update_value(False)
                    detail = bridge.refresh_status("second")
                    self.assertEqual(detail["controller_write_count"], 1)
                    self.assertFalse(detail["last_controller_write_value"])
                    self.assertGreaterEqual(detail["last_controller_write_at"], before_write)
            finally:
                await bridge.close()
                await bridge.close()
            self.assertEqual(len(json.loads((root / "accessory-ids.json").read_text())), 2)

    async def test_invalid_saved_pin_is_not_replaced_or_used_to_start(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            code = root / "pairing-code.txt"
            code.write_text("invalid\n")
            bridge = RefreshBridge(AppleHomeSettings(hap_port=free_port()), root, {"sensor": "读数"})
            with self.assertRaisesRegex(ValueError, "Invalid saved HomeKit pairing code"):
                await bridge.start()
            self.assertIsNone(bridge.driver)
            self.assertEqual(code.read_text(), "invalid\n")
