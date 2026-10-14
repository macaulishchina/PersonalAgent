"""Explicit real-device onboarding acceptance: microphone, speaker and Codex tools.

Stops an idle installed voice instance, uses an isolated state/thread, plays a
short authored chime, receives mic PCM in memory only, and restores the service.
No raw recording is saved. Results stay under ignored state/acceptance/.
"""

import argparse
import asyncio
import dataclasses
import json
from pathlib import Path
import tempfile
import time

import numpy as np
import miniaudio
import soxr

from assistant_runtime.assistant.service import AssistantService
from assistant_runtime.cli import request, systemctl
from assistant_runtime.config import Config
from assistant_runtime.kernel.contracts import DeviceQuery
from assistant_runtime.plugins.pipewire.audio import command
from assistant_runtime.plugins.pipewire.cues import wake_chime


def correlate(recorded, reference):
    x, y = recorded.astype(float), reference.astype(float)
    x -= x.mean()
    y -= y.mean()
    size = 1 << (len(x) + len(y) - 1).bit_length()
    corr = np.fft.irfft(np.fft.rfft(x, size) * np.fft.rfft(y[::-1], size), size)[len(y) - 1 : len(x)]
    energy = np.concatenate(([0.0], np.cumsum(x * x)))
    denominator = np.sqrt((energy[len(y) :] - energy[: -len(y)]) * np.sum(y * y))
    values = np.abs(corr) / np.maximum(denominator, 1e-9)
    offset = int(np.argmax(values))
    return float(values[offset]), offset / 16000


async def verify(config, folder, disconnect, output_device=None, tail_seconds=3, speech_sample=None):
    before = await request(config, {"command": "status"})
    if before["task_running"] or before["mode"] != "standby":
        raise RuntimeError("Wait until the assistant is idle before explicit hardware acceptance")
    target = output_device or before["room"]
    if target not in config.output_devices:
        raise ValueError("Unknown output device")
    report = {"started_at": time.time(), "input_device": config.input_device, "output_device": target}
    folder.mkdir(parents=True, exist_ok=True)
    systemctl("stop", config.unit_name)
    try:
        with tempfile.TemporaryDirectory(prefix="hw-", dir=config.state_dir) as temp:
            isolated = dataclasses.replace(
                config,
                root=Path(temp),
                workspace=temp,
                model_dir=str(config.models),
                memory_enabled=False,
                memory_auto_capture=False,
            )
            service = AssistantService(isolated, no_mic=True)
            events = service.runtime.subscribe("onboarding")
            run = asyncio.create_task(service.run())
            capture = None
            pcm = bytearray()
            try:

                async def wait(predicate, seconds=30):
                    deadline = time.monotonic() + seconds
                    while not predicate():
                        if run.done():
                            run.result()
                            raise RuntimeError("Acceptance host exited")
                        if time.monotonic() > deadline:
                            raise TimeoutError("Acceptance condition not reached")
                        await asyncio.sleep(0.1)

                await wait(
                    lambda: getattr(service, "context", None) is not None and service.context.state == "ready"
                )
                await service.route.select(target)
                inventory = await request(isolated, {"command": "devices", "action": "list"})
                mic = await request(
                    isolated, {"command": "devices", "action": "inspect", "device_id": config.input_device}
                )
                output = await request(
                    isolated, {"command": "devices", "action": "inspect", "device_id": target}
                )
                assert mic["device"]["state"] == output["device"]["state"] == "available"
                report["inventory"] = inventory
                report["initial_input"] = mic
                report["initial_output"] = output

                async def receive():
                    async for frame in service.route.frames():
                        pcm.extend(service.route.raw_frame or frame)

                capture = asyncio.create_task(receive())
                await wait(lambda: len(pcm) >= 16000, 12)
                report["active_capture"] = await service.runtime.invoke(
                    DeviceQuery("inspect", config.input_device)
                )
                assert report["active_capture"]["device"]["data"]["capturing"]
                await service.player.cue()
                await asyncio.wait_for(service.player.queue.join(), 12)
                await asyncio.sleep(tail_seconds)
                if speech_sample:
                    decoded = miniaudio.decode_file(
                        str(speech_sample),
                        output_format=miniaudio.SampleFormat.SIGNED16,
                        nchannels=1,
                        sample_rate=24000,
                    )
                    speech = decoded.samples.tobytes()
                    if len(speech) > 15 * 48000:
                        raise ValueError("Explicit speech sample must be no more than 15 seconds")
                    start = time.monotonic()
                    for index, offset in enumerate(range(0, len(speech), 960)):
                        await asyncio.sleep(max(0, start + index * 0.02 - time.monotonic()))
                        service.player.feed(speech[offset : offset + 960].ljust(960, b"\0"))
                    await service.player.queue.join()
                    await asyncio.sleep(3)
                    report["speech_test"] = {
                        "sample": str(speech_sample),
                        "audio_seconds": len(speech) / 48000,
                    }
                capture.cancel()
                await asyncio.gather(capture, return_exceptions=True)
                capture = None
                samples = np.frombuffer(pcm, dtype="<i2").astype(float)
                reference = soxr.resample(
                    np.frombuffer(wake_chime(), dtype="<i2").astype(float), 24000, 16000
                )
                coefficient, offset = correlate(samples, reference)
                report["acoustic_loop"] = {
                    "capture_seconds": len(samples) / 16000,
                    "microphone_rms": float(np.sqrt(np.mean(samples * samples))),
                    "chime_correlation": coefficient,
                    "match_offset_s": offset,
                    "written_bytes": service.player.written_bytes,
                    "raw_audio_saved": False,
                }
                report["player_error"] = service.player.last_error
                report["output_after_play"] = await service.runtime.invoke(DeviceQuery("inspect", target))
                assert coefficient >= 0.10, "Chime was not independently confirmed in the physical microphone"
                assert service.player.written_bytes > len(wake_chime())
                await service.player.stop(suppress=False)
                if disconnect:
                    address = config.devices[target]["connection"].get("address")
                    if not address:
                        raise ValueError("Disconnect acceptance requires a Bluetooth output")
                    service.route.last_connect = time.monotonic()
                    await command("bluetoothctl", "disconnect", address)
                    deadline = time.monotonic() + 10
                    while time.monotonic() < deadline:
                        missing = await service.runtime.invoke(DeviceQuery("inspect", target))
                        if missing["device"]["state"] == "unavailable":
                            break
                        await asyncio.sleep(0.2)
                    assert missing["device"]["state"] == "unavailable"
                    report["disconnected_output"] = missing
                    assert service.route.room == target
                    assert (await service.runtime.invoke(DeviceQuery("inspect", config.input_device)))[
                        "device"
                    ]["state"] == "available"
                    await command("bluetoothctl", "connect", address, timeout=15)

                    async def recover():
                        while True:
                            try:
                                await service.route.ensure(force=True)
                                return
                            except (RuntimeError, TimeoutError):
                                await asyncio.sleep(0.5)

                    await asyncio.wait_for(recover(), 30)
                    restored = await service.runtime.invoke(DeviceQuery("inspect", target))
                    assert restored["device"]["state"] == "available"
                    report["restored_output"] = restored
                # Clear earlier direct query observations; subsequent evidence is
                # from actual Codex -> MCP -> socket -> capability -> adapter.
                while not events.empty():
                    events.get_nowait()
                    events.task_done()
                await service.backend.submit(
                    f"本次只进行只读设备验收。先使用 device MCP 的 list_devices；再分别使用 inspect_device，device_id={config.input_device} 和 device_id={target}。必须实际调用这三项查询，不用 shell，不调用 memory，不改设备，不执行其他任务。最后简述两台设备的名称、能力和在线状态。"
                )
                await wait(lambda: not service.backend.busy, 120)
                queries = []
                while not events.empty():
                    event = events.get_nowait()
                    events.task_done()
                    if event.kind == "device.queried":
                        queries.append(event.data)
                assert any(q["action"] == "list" for q in queries), "Codex did not list devices"
                for ident in (config.input_device, target):
                    assert any(q["action"] == "inspect" and q["device_id"] == ident for q in queries), (
                        f"Codex did not inspect {ident}"
                    )
                report["codex_tool_queries"] = queries
                report["codex_result"] = service.store.recent(1)
                report["passed"] = True
            finally:
                if capture:
                    capture.cancel()
                    await asyncio.gather(capture, return_exceptions=True)
                service.stop_event.set()
                await asyncio.wait_for(run, 30)
    except BaseException as exc:
        report["passed"] = False
        report["error"] = str(exc)
        raise
    finally:
        systemctl("start", config.unit_name)
        report["finished_at"] = time.time()
        (folder / "real-devices.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--config", default="config.toml")
    p.add_argument("--disconnect", action="store_true")
    p.add_argument("--output-device")
    p.add_argument("--tail-seconds", type=float, default=3)
    p.add_argument("--speech-sample", type=Path)
    args = p.parse_args()
    config = Config.load(args.config)
    asyncio.run(
        verify(
            config,
            config.state_dir
            / "acceptance"
            / ("airplay-homepod" if args.output_device else "device-onboarding"),
            args.disconnect,
            args.output_device,
            args.tail_seconds,
            args.speech_sample,
        )
    )


if __name__ == "__main__":
    main()
