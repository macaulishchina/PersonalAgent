import copy
from pathlib import Path
import tempfile
import unittest

from assistant_runtime.plugins.reyee.protocol import parse_clients
from assistant_runtime.plugins.adb_tv.protocol import parse_power
from assistant_runtime.plugins.reyee.snapshot import Snapshot
from assistant_runtime.plugins.reyee.events import ReyeeEvents
from assistant_runtime.plugins.observations.registry import Registry


# Synthetic redacted protocol fixture, shaped like ReyeeOS 1.380.1307.
FIXTURE = {
    "code": 0,
    "error": None,
    "data": {
        "code": "0",
        "total": "2",
        "list": [
            {
                "mac": "02:00:00:00:00:01",
                "userIp": "192.0.2.10",
                "connectType": "wireless",
                "ssid": "test",
                "band": "5G",
                "sn": "test-ap",
                "onlinetime": "untrusted local clock",
                "future_field": 123,
            },
            {"mac": "02:00:00:00:00:02", "userIp": "192.0.2.11", "connectType": "wire", "sn": "test-router"},
        ],
    },
}


class SourceTest(unittest.TestCase):
    def test_full_wired_wireless_snapshot_and_invalid_lists(self):
        parsed = parse_clients(FIXTURE)
        self.assertEqual(len(parsed), 2)
        self.assertNotIn("future_field", parsed["02:00:00:00:00:01"])
        for payload in (
            {"code": 1},
            {"code": 0, "data": {"code": "0", "list": [], "total": "2"}},
            {"code": 0, "data": {"code": "0", "total": "0"}},
        ):
            with self.assertRaises(ValueError):
                parse_clients(payload)
        duplicate = copy.deepcopy(FIXTURE)
        duplicate["data"]["list"][1]["mac"] = duplicate["data"]["list"][0]["mac"]
        with self.assertRaises(ValueError):
            parse_clients(duplicate)

    def test_baseline_two_successful_absences_recovery_and_changed_attributes(self):
        current = parse_clients(FIXTURE)
        tracker = Snapshot()
        first = tracker.prepare(current, 100)
        self.assertTrue(all(event[0] == "lan.baseline" for event in first[0]))
        tracker.commit(first, 100)
        self.assertEqual(tracker.prepare(current, 110)[0], [])
        absent = tracker.prepare({}, 120)
        self.assertEqual(absent[0], [])
        tracker.commit(absent, 120)
        gone = tracker.prepare({}, 130)
        self.assertEqual([r[0] for r in gone[0]], ["lan.disappeared"] * 2)
        self.assertEqual(gone[3], 100)
        tracker.commit(gone, 130)
        appeared = tracker.prepare(current, 140)
        self.assertEqual([r[0] for r in appeared[0]], ["lan.appeared"] * 2)
        tracker.commit(appeared, 140)
        changed = copy.deepcopy(current)
        changed["02:00:00:00:00:01"]["userIp"] = "192.0.2.20"
        self.assertEqual(tracker.prepare(changed, 150)[0][0][0], "lan.changed")
        recovery = Snapshot().prepare(current, 160)
        self.assertTrue(all(event[0] == "lan.baseline" for event in recovery[0]))

    def test_tv_semantics_never_turn_disconnect_into_off(self):
        self.assertEqual(parse_power("mWakefulness=Awake\nDisplay Power: state=ON")["state"], "on")
        self.assertEqual(parse_power("mWakefulness=Asleep\nmGlobalDisplayState=OFF")["state"], "standby")
        self.assertEqual(parse_power("mWakefulness=Awake\nmGlobalDisplayState=OFF")["state"], "display_off")
        with self.assertRaises(ValueError):
            parse_power("device offline")
        self.assertEqual(parse_power("mWakefulness=Unknown\nmGlobalDisplayState=OFF")["state"], "unknown")

    def test_registry_only_trusted_config_revision_and_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            registry = Registry(Path(folder), {"reyee": ReyeeEvents(Path(folder))})
            registry.load()
            registry.update(
                "configure",
                "router",
                0,
                {
                    "adapter": "reyee",
                    "enabled": True,
                    "config": {"url": "http://192.0.2.1", "credential": "router"},
                },
            )
            with self.assertRaises(ValueError):
                registry.update("enable", "router", 0, enabled=False)
            restored = Registry(Path(folder), {"reyee": ReyeeEvents(Path(folder))})
            self.assertEqual(restored.load()["revision"], 1)
            restored.update("remove", "router", 1)
            self.assertEqual(restored.sources, {})
            with self.assertRaises(ValueError):
                registry.validate({"adapter": "shell", "config": {"command": "anything"}, "enabled": True})
            with self.assertRaises(ValueError):
                ReyeeEvents(Path(folder)).validate({"url": "http://192.0.2.1", "credential": "../../secret"})


class ProtocolTest(unittest.IsolatedAsyncioTestCase):
    async def test_expired_session_reauth_and_failure_redaction(self):
        from assistant_runtime.plugins.reyee.protocol import Reyee
        import json

        class Router(Reyee):
            def __init__(self):
                super().__init__({}, Path("/unused"))
                self.logins = 0
                self.paths = []
                self.expired = True

            async def login(self):
                self.logins += 1
                self.sid = "private-session"

            async def request(self, path, payload=None):
                self.paths.append((path, payload))
                if self.expired:
                    self.expired = False
                    return json.dumps({"code": 401, "error": "private-session"})
                return json.dumps(FIXTURE)

        router = Router()
        with self.assertRaises(RuntimeError) as raised:
            await router.sample()
        self.assertNotIn("private-session", str(raised.exception))
        self.assertIsNone(router.sid)
        self.assertEqual(len(await router.sample()), 2)
        self.assertEqual(router.logins, 2)
        self.assertTrue(
            all(
                payload["method"] == "devSta.get" and payload["params"]["module"] == "user_list"
                for _, payload in router.paths
            )
        )
