"""Disconnect only the selected speaker, observe reconnect and check no task replay."""
import asyncio
import json
import sqlite3
import time

from dell_voice.audio import command
from dell_voice.cli import request
from dell_voice.config import Config


async def main():
    c = Config.load()
    async def status(): return await request(c, {"command":"status"})
    async def ready(timeout=35):
        end = time.monotonic()+timeout
        while time.monotonic() < end:
            try:
                s = await status()
                if s["audio_ready"] and s["capture_recent"]: return s
            except (OSError, RuntimeError): pass
            await asyncio.sleep(.5)
        raise TimeoutError("Device did not recover")
    def task_count():
        with sqlite3.connect(c.state_dir/"assistant.sqlite3") as db:
            return db.execute("select count(*) from tasks").fetchone()[0]
    initial = await ready()
    assert initial["mode"] == "standby" and not initial["task_running"]
    count = task_count()
    started = time.monotonic()
    disconnected = await command("bluetoothctl", "disconnect", c.speakers[initial["room"]])
    samples = []
    # Let the service observe disappearing audio nodes and reconnect itself.
    for _ in range(20):
        s = await status()
        samples.append({"seconds": round(time.monotonic()-started,2), "ready":s["audio_ready"], "capture":s["capture_recent"], "room":s["room"]})
        assert s["room"] == initial["room"]
        await asyncio.sleep(.5)
    final = await ready()
    assert count == task_count()
    result = {"passed": True, "disconnect_acknowledged": "Successful disconnected" in disconnected,
              "samples": samples, "final": final, "task_count_unchanged": True}
    (c.state_dir/"acceptance/device-recovery.json").write_text(json.dumps(result,ensure_ascii=False,indent=2))
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__ == "__main__": asyncio.run(main())
