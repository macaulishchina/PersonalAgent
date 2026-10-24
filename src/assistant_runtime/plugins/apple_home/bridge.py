"""Upstream HAP-python owns pairing/encryption/events; this owns refresh signals."""

from assistant_runtime.api.system import system

import asyncio
import json
import logging
from pathlib import Path
import re
import time

from pyhap.accessory import Accessory, Bridge
from pyhap.accessory_driver import AccessoryDriver, get_topic
from pyhap.const import CATEGORY_LIGHTBULB

log = logging.getLogger(__name__)


class PrivateBridge(Bridge):
    def setup_message(self):
        # Pairing material belongs only in the private instance directory.
        return


class RefreshLamp(Accessory):
    category = CATEGORY_LIGHTBULB

    def __init__(self, driver, name, aid):
        super().__init__(driver, name, aid=aid)
        self.on = self.add_preload_service("Lightbulb").get_characteristic("On")
        self.controller_write_count = 0
        self.last_controller_write_at = None
        self.last_controller_write_value = None
        self.on.setter_callback = self.controller_write

    def controller_write(self, value):
        # Upstream invokes this for controller writes, including an unchanged
        # value; our internal set_value pulses must not count as inbound control.
        self.controller_write_count += 1
        self.last_controller_write_at = time.time()
        self.last_controller_write_value = bool(value)
        log.info("HomeKit controller wrote refresh lamp: aid=%s on=%s", self.aid, bool(value))


class RefreshBridge:
    def __init__(self, settings, root: Path, endpoints):
        self.settings, self.root, self.endpoints = settings, root, endpoints
        self.driver = None
        self.lamps = {}
        self.resets = set()
        self.started = False

    @property
    def paired(self):
        return bool(self.driver and self.driver.state.paired)

    async def start(self):
        self.root.mkdir(parents=True, mode=0o700, exist_ok=True)
        code = self.root / "pairing-code.txt"
        if code.is_symlink():
            raise ValueError("HomeKit pairing code cannot be a symlink")
        pincode = code.read_bytes().strip() if code.exists() else None
        if pincode is not None and not re.fullmatch(rb"[0-9]{3}-[0-9]{2}-[0-9]{3}", pincode):
            raise ValueError("Invalid saved HomeKit pairing code")
        # HAP-python persists pairing keys/clients, but not the setup PIN.
        # Reuse our private copy so instructions survive service restarts.
        self.driver = AccessoryDriver(
            address=self.settings.host,
            port=self.settings.hap_port,
            persist_file=str(self.root / "hap.state"),
            pincode=pincode,
            loop=asyncio.get_running_loop(),
        )
        bridge = PrivateBridge(self.driver, "环境读数桥接")
        # Persist identities: inserting/removing an endpoint must not redirect
        # an existing Apple Home automation to a different refresh accessory.
        path = self.root / "accessory-ids.json"
        identities = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        if (
            not isinstance(identities, dict)
            or any(type(v) is not int or v < 2 or v == 7 for v in identities.values())
            or len(set(identities.values())) != len(identities)
        ):
            raise ValueError("Invalid persisted HomeKit accessory identities")
        for ident in sorted(self.endpoints):
            if ident not in identities:
                aid = 2
                while aid == 7 or aid in identities.values():
                    aid += 1
                identities[ident] = aid
            lamp = RefreshLamp(self.driver, self.endpoints[ident], identities[ident])
            bridge.add_accessory(lamp)
            self.lamps[ident] = lamp
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(identities), encoding="utf-8")
        system.private(temporary)
        temporary.replace(path)
        self.driver.add_accessory(bridge)
        system.private(self.root / "hap.state")
        code.write_text(self.driver.state.pincode.decode() + "\n", encoding="utf-8")
        system.private(code)
        try:
            await self.driver.async_start()
            self.started = True
        except BaseException:
            await self.close()
            raise

    def pulse(self, ident):
        if not self.started or not self.paired:
            return False
        log.info(
            "HomeKit refresh requested for %s: subscribers=%d",
            ident,
            self.refresh_status(ident)["subscriber_count"],
        )
        lamp = self.lamps[ident]
        lamp.on.set_value(True)
        handle = None

        def reset():
            self.resets.discard(handle)
            if self.started:
                lamp.on.set_value(False)

        handle = asyncio.get_running_loop().call_later(1, reset)
        self.resets.add(handle)
        return True

    def refresh_status(self, ident):
        lamp = self.lamps.get(ident)
        if not self.driver or not lamp:
            return {"on": False, "subscriber_count": 0}
        topic = get_topic(lamp.aid, lamp.iid_manager.get_iid(lamp.on))
        return {
            "on": bool(lamp.on.value),
            "subscriber_count": len(self.driver.topics.get(topic, ())),
            "controller_write_count": lamp.controller_write_count,
            "last_controller_write_at": lamp.last_controller_write_at,
            "last_controller_write_value": lamp.last_controller_write_value,
        }

    async def close(self):
        self.started = False
        for handle in self.resets:
            handle.cancel()
        self.resets.clear()
        driver, self.driver = self.driver, None
        if not driver:
            return
        if driver.advertiser:
            if driver.mdns_service_info:
                try:
                    await driver.advertiser.async_unregister_service(driver.mdns_service_info)
                except Exception:
                    pass
            await driver.advertiser.async_close()
        if driver.aio_stop_event:
            driver.aio_stop_event.set()
        driver.stop_event.set()
        driver.http_server.async_stop()
        if driver.accessory:
            await driver.async_add_job(driver.accessory.stop)
