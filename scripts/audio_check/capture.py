"""Standalone capture using existing device registries; no service-side test mode."""

import asyncio
import json
import math
import os
import time

from assistant_runtime.core.devices.inputs import AudioInputs
from assistant_runtime.plugins.pipewire.audio import audio_env
from assistant_runtime.plugins.pipewire.io import PipeWireEndpoint

from .metrics import FRAME_BYTES, RATE, SignalStats, checks
from .prepare import command


def duration(value):
    if type(value) not in (int, float) or not 1 <= value <= 30 or not math.isfinite(value):
        raise ValueError("录音时长必须在1到30秒之间。")
    return float(value)


async def require_stopped(config):
    env = audio_env()
    env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path=/run/user/{os.getuid()}/bus"
    output = await command(
        ["systemctl", "--user", "show", config.unit_name, "-p", "ActiveState", "-p", "MainPID"],
        env=env,
        timeout=5,
    )
    state = dict(line.split("=", 1) for line in output.decode(errors="replace").splitlines() if "=" in line)
    if state.get("ActiveState") not in {"inactive", "failed"} or state.get("MainPID") != "0":
        raise RuntimeError("请先停止助手服务再运行独立测试；脚本不会替你停启服务。")
    try:
        _, writer = await asyncio.wait_for(asyncio.open_unix_connection(config.socket), 1)
    except (ConnectionRefusedError, FileNotFoundError):
        return
    writer.close()
    await writer.wait_closed()
    raise RuntimeError("管理接口仍在监听，可能有手动启动的助手；不会抢占麦克风。")


def endpoint_fingerprint(config):
    import hashlib

    specs = [config.devices[key] for key in (config.input_device, config.default_output)]
    return hashlib.sha256(json.dumps(specs, sort_keys=True).encode()).hexdigest()


class Recording:
    def __init__(self, seconds):
        self.seconds = duration(seconds)
        self.target = round(self.seconds * RATE) * 2
        self.pcm = bytearray()
        self.signal = SignalStats()
        self.started = self.last = time.monotonic()
        self.max_gap = 0.0
        self.done = asyncio.get_running_loop().create_future()

    def feed(self, pcm):
        now = time.monotonic()
        self.max_gap = max(self.max_gap, now - self.last)
        self.last = now
        pcm = pcm[: self.target - len(self.pcm)]
        self.pcm.extend(pcm)
        self.signal.add(pcm)
        if len(self.pcm) == self.target:
            self.done.set_result(None)

    def report(self):
        measured = {
            "sample_rate": RATE,
            "sample_format": "s16le",
            "channels": 1,
            "requested_seconds": self.seconds,
            "signal": self.signal.report(),
            "stream": {
                "elapsed_seconds": round(self.last - self.started, 4),
                "longest_delivery_gap_ms": round(self.max_gap * 1000, 3),
            },
            "scope": "independent_raw_capture_no_software_processing",
        }
        return bytes(self.pcm), measured, checks(measured)


class Capture:
    def __init__(self, config):
        spec = config.devices[config.input_device]
        if spec["adapter"] != "pipewire":
            raise ValueError("独立测试暂支持 PipeWire 输入，不会自动换设备。")
        self.config = config
        self.inputs = AudioInputs(config.devices)
        self.inputs.register("pipewire", config.input_device, PipeWireEndpoint(spec))
        self.task = self.current = None

    async def start(self):
        await require_stopped(self.config)
        stream = await self.inputs.open(self.config.input_device, RATE, owner="audio-input-check")

        async def pump():
            while True:
                pcm = await asyncio.wait_for(stream.read(FRAME_BYTES), 3)
                if self.current is not None and not self.current.done.done():
                    self.current.feed(pcm)

        self.task = asyncio.create_task(pump(), name="benchmark-capture")

    async def ready(self):
        await require_stopped(self.config)
        if self.task is None:
            raise RuntimeError("录音尚未打开。")
        if self.task.done():
            self.task.result()
            raise RuntimeError("采集流已结束。")

    async def record(self, seconds):
        await self.ready()
        if self.current is not None:
            raise RuntimeError("已有录音窗口，不会并发采音。")
        recording = self.current = Recording(seconds)
        try:
            async with asyncio.timeout(recording.seconds + 5):
                while not recording.done.done():
                    await asyncio.wait(
                        (recording.done, self.task), timeout=1, return_when=asyncio.FIRST_COMPLETED
                    )
                    await self.ready()
            await recording.done
            return recording.report()
        finally:
            self.current = None
            if not recording.done.done():
                recording.done.cancel()

    async def close(self):
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        await self.inputs.close()
