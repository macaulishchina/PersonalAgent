"""Upstream HAP-python owns pairing/encryption/events; this owns refresh signals."""

import asyncio
import json
from pathlib import Path

from pyhap.accessory import Accessory, Bridge
from pyhap.accessory_driver import AccessoryDriver
from pyhap.const import CATEGORY_LIGHTBULB


class PrivateBridge(Bridge):
    def setup_message(self):
        # Pairing material belongs only in the private instance directory.
        return


class RefreshLamp(Accessory):
    category = CATEGORY_LIGHTBULB

    def __init__(self, driver, name, aid):
        super().__init__(driver, name, aid=aid)
        self.on = self.add_preload_service("Lightbulb").get_characteristic("On")


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
        self.driver = AccessoryDriver(
            address=self.settings.host,
            port=self.settings.hap_port,
            persist_file=str(self.root / "hap.state"),
            loop=asyncio.get_running_loop(),
        )
        bridge = PrivateBridge(self.driver, "环境读数桥接")
        # Persist identities: inserting/removing an endpoint must not redirect
        # an existing Apple Home automation to a different refresh accessory.
        path = self.root / "accessory-ids.json"
        identities = json.loads(path.read_text()) if path.exists() else {}
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
        temporary.write_text(json.dumps(identities))
        temporary.chmod(0o600)
        temporary.replace(path)
        self.driver.add_accessory(bridge)
        (self.root / "hap.state").chmod(0o600)
        code = self.root / "pairing-code.txt"
        code.write_text(self.driver.state.pincode.decode() + "\n")
        code.chmod(0o600)
        try:
            await self.driver.async_start()
            self.started = True
        except BaseException:
            await self.close()
            raise

    def pulse(self, ident):
        if not self.started or not self.paired:
            return False
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
