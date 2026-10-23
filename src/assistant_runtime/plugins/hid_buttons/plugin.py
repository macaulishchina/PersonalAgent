"""Read-only Linux HID trigger adapter with bounded work and reconnects."""

from .declaration import DECLARATION

import asyncio
import logging
import os
from pathlib import Path
import time

from assistant_runtime.api.contracts import Trigger
from .settings import parse

log = logging.getLogger(__name__)


def discover(button, root=Path("/sys/class/hidraw"), devices=Path("/dev")):
    matches = []
    for entry in root.glob("hidraw*"):
        try:
            fields = dict(
                line.split("=", 1)
                for line in (entry / "device/uevent").read_text().splitlines()
                if "=" in line
            )
            bus, vendor, product = (int(value, 16) for value in fields["HID_ID"].split(":"))
            if (
                bus == 3
                and (vendor, product) == (button.vendor_id, button.product_id)
                and fields.get("HID_UNIQ") == button.serial
            ):
                matches.append(devices / entry.name)
        except (OSError, KeyError, ValueError):
            continue
    if len(matches) != 1:
        raise OSError("HID device absent or identity ambiguous")
    return matches[0]


class HidButtonsPlugin:
    manifest = DECLARATION.manifest

    def __init__(self, config):
        self.buttons = parse(config.hid_buttons)
        self.context = None
        self.states = {button.name: "starting" for button in self.buttons}
        self.last_report = {}
        self.pending = {}

    async def start(self, context):
        self.context = context
        for button in self.buttons:
            context.spawn(self.watch(button), button.name)

    def health(self, name, state):
        if self.states[name] != state:
            self.states[name] = state
            log.info("hid_button_state name=%s state=%s", name, state)
        failures = {key: value for key, value in self.states.items() if value != "ready"}
        self.context.set_health(
            "degraded" if failures else "ready", str(failures)[:240] if failures else None
        )

    def report(self, button, packet, now=None):
        if packet != button.report or not self.context.ready.is_set() or self.context.cancelled.is_set():
            return
        now = time.monotonic() if now is None else now
        previous = self.last_report.get(button.name, float("-inf"))
        self.last_report[button.name] = now
        pending = self.pending.get(button.name)
        if now - previous < button.debounce_ms / 1000 or (pending and not pending.done()):
            return
        self.pending[button.name] = self.context.spawn(self.wake(button), "wake:" + button.name)

    async def wake(self, button):
        try:
            result = await self.context.invoke(Trigger("hid_buttons:" + button.name, "wake"))
            log.info("hid_button_wake name=%s accepted=%s", button.name, bool(result.get("accepted")))
        except Exception as exc:
            # Never retry a wake: the request may already have reached the owner.
            log.warning("hid_button_wake_failed name=%s error=%s", button.name, type(exc).__name__)

    async def watch(self, button):
        await self.context.ready.wait()
        while True:
            try:
                path = discover(button)
                fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
                try:
                    # Detect replacement between identity lookup and opening the node.
                    opened, current = os.fstat(fd), path.stat()
                    if discover(button) != path or (opened.st_dev, opened.st_ino, opened.st_rdev) != (
                        current.st_dev,
                        current.st_ino,
                        current.st_rdev,
                    ):
                        raise OSError("HID identity changed while opening")
                    await self.read(button, fd)
                finally:
                    os.close(fd)
            except OSError as exc:
                self.health(
                    button.name, "permission_denied" if isinstance(exc, PermissionError) else "disconnected"
                )
            await asyncio.sleep(2)

    async def read(self, button, fd):
        loop = asyncio.get_running_loop()
        done = loop.create_future()

        def readable():
            try:
                # Bound each callback so a noisy HID cannot monopolize the event loop.
                for _ in range(32):
                    packet = os.read(fd, 4096)
                    if not packet:
                        raise OSError("HID disconnected")
                    self.report(button, packet)
            except BlockingIOError:
                pass
            except OSError as exc:
                loop.remove_reader(fd)
                if not done.done():
                    done.set_exception(exc)

        loop.add_reader(fd, readable)
        self.health(button.name, "ready")
        try:
            await done
        finally:
            loop.remove_reader(fd)
