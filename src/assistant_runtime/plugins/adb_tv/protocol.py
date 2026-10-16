"""Read-only device protocol; failures never mean an empty snapshot."""

import asyncio
import re


def parse_power(text):
    wake = re.search(r"mWakefulness=(\w+)", text)
    display = re.search(r"(?:Display Power: state|Display Power state|mGlobalDisplayState)=(\w+)", text)
    if not wake or not display:
        raise ValueError("Unrecognized ADB power evidence")
    if wake[1] in {"Asleep", "Dozing"} and display[1] == "OFF":
        state = "standby"
    elif wake[1] == "Awake" and display[1] == "ON":
        state = "on"
    elif wake[1] == "Awake" and display[1] == "OFF":
        state = "display_off"
    else:
        state = "unknown"
    return {"state": state, "wakefulness": wake[1], "display": display[1]}


class AdbTV:
    def __init__(self, config, directory):
        self.config = config

    async def open(self):
        pass

    async def close(self):
        pass

    async def command(self, *args):
        process = await asyncio.create_subprocess_exec(
            "adb", *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
        )
        try:
            async with asyncio.timeout(4):
                chunks, total = [], 0
                while chunk := await process.stdout.read(16384):
                    total += len(chunk)
                    if total > 256 * 1024:
                        raise ValueError("ADB response exceeds limit")
                    chunks.append(chunk)
                raw = b"".join(chunks)
                if len(raw) > 256 * 1024:
                    raise ValueError("ADB response exceeds limit")
                await process.wait()
            if process.returncode:
                raise ValueError("ADB state unavailable")
            return raw.decode("utf-8", errors="replace")
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()

    async def sample(self):
        await self.command("connect", self.config["address"])
        return parse_power(await self.command("-s", self.config["address"], "shell", "dumpsys", "power"))
