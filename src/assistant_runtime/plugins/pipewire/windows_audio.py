"""WASAPI through pinned miniaudio; endpoint volume through Core Audio."""

import asyncio
import copy
import re
import threading


def validate(spec):
    if spec["kind"] not in {"microphone", "speaker"}:
        raise ValueError("WASAPI supports microphone and speaker endpoints")
    connection = spec["connection"]
    if set(connection) != {"endpoint_id"} or not re.fullmatch(
        r"\{0\.[01]\.0\.00000000\}\.\{[0-9a-fA-F-]{36}\}", str(connection.get("endpoint_id", ""))
    ):
        raise ValueError("WASAPI requires an exact Core Audio endpoint_id; no default endpoint")
    capture = connection["endpoint_id"].startswith("{0.1.")
    if capture != (spec["kind"] == "microphone"):
        raise ValueError("WASAPI endpoint identity and device kind differ")


def endpoints(kind):
    import miniaudio

    inventory = miniaudio.Devices(backends=[miniaudio.Backend.WASAPI])
    rows = inventory.get_captures() if kind == "microphone" else inventory.get_playbacks()
    result = []
    for row in rows:
        # The pinned WASAPI ABI stores its UTF-16 endpoint ID at the start of ma_device_id.
        identity = bytes(miniaudio.ffi.buffer(row["id"], miniaudio.ffi.sizeof("ma_device_id")))
        identifier = identity.decode("utf-16-le").split("\0", 1)[0]
        result.append({**row, "endpoint_id": identifier})
    return result


def resolve(spec):
    rows = [row for row in endpoints(spec["kind"]) if row["endpoint_id"] == spec["connection"]["endpoint_id"]]
    if len(rows) != 1:
        raise OSError("Configured WASAPI endpoint unavailable or ambiguous; no fallback")
    return rows[0]


def volume(identifier, value=None):
    import comtypes
    from pycaw.pycaw import AudioUtilities

    comtypes.CoInitialize()
    try:
        device = AudioUtilities.GetDeviceEnumerator().GetDevice(identifier)
        if device.GetState() != 1:
            raise OSError("Configured audio endpoint is not active")
        control = AudioUtilities.CreateDevice(device).EndpointVolume
        if value is not None:
            control.SetMasterVolumeLevelScalar(value / 100, None)
            control.SetMute(value == 0, None)
        actual = round(control.GetMasterVolumeLevelScalar() * 100)
        muted = bool(control.GetMute())
        if value is not None and (abs(actual - value) > 1 or muted != (value == 0)):
            raise OSError("Windows did not confirm the requested endpoint volume")
        return actual, muted
    finally:
        comtypes.CoUninitialize()


class Transport:
    def __init__(self, spec, rate):
        import miniaudio

        self.capture = spec["kind"] == "microphone"
        self.rate, self.maximum = rate, rate * 2  # One second, mono PCM16.
        self.buffer = bytearray()
        self.lock = threading.Lock()
        self.error = None
        self.closed = False
        row = resolve(spec)
        factory = miniaudio.CaptureDevice if self.capture else miniaudio.PlaybackDevice
        self.device = factory(
            nchannels=1,
            sample_rate=rate,
            buffersize_msec=20,
            device_id=row["id"],
            backends=[miniaudio.Backend.WASAPI],
            app_name="Personal Agent",
        )
        callback = self.record() if self.capture else self.play()
        next(callback)
        try:
            self.device.start(callback)
        except BaseException:
            self.device.close()
            raise

    @property
    def healthy(self):
        return not self.closed and self.error is None and self.device.running

    def record(self):
        data = yield
        while True:
            with self.lock:
                if len(self.buffer) + len(data) > self.maximum:
                    self.error = "Audio capture overrun; data discarded"
                    self.buffer.clear()
                elif not self.error and not self.closed:
                    self.buffer.extend(data)
            data = yield

    def play(self):
        frames = yield b""
        while True:
            needed = frames * 2
            with self.lock:
                count = min(needed, len(self.buffer)) if not self.error and not self.closed else 0
                data = bytes(self.buffer[:count])
                del self.buffer[:count]
            frames = yield data + bytes(needed - count)

    async def read(self, size):
        if not self.capture or type(size) is not int or not 0 < size <= self.maximum or size % 2:
            raise ValueError("Invalid PCM16 capture read")
        while self.healthy:
            with self.lock:
                if len(self.buffer) >= size:
                    data = bytes(self.buffer[:size])
                    del self.buffer[:size]
                    return data
            await asyncio.sleep(0.01)
        raise OSError(self.error or "Audio input closed")

    async def write(self, pcm):
        if self.capture or not isinstance(pcm, bytes) or len(pcm) != self.rate // 50 * 2:
            raise ValueError("Playback requires one 20ms mono PCM16 frame")
        async with asyncio.timeout(2):
            while self.healthy:
                with self.lock:
                    if len(self.buffer) + len(pcm) <= self.maximum:
                        self.buffer.extend(pcm)
                        return
                await asyncio.sleep(0.01)
        raise OSError(self.error or "Audio output closed")

    async def close(self):
        if not self.closed:
            self.closed = True
            with self.lock:
                self.buffer.clear()
            await asyncio.to_thread(self.device.close)


class Endpoint:
    def __init__(self, spec, minimum_volume=0):
        validate(spec)
        self.spec = copy.deepcopy(spec)
        self.minimum_volume, self.volume = minimum_volume, None
        self.transports = []

    async def ensure_ready(self):
        await asyncio.to_thread(resolve, self.spec)

    async def open(self, rate):
        opening = asyncio.create_task(asyncio.to_thread(Transport, self.spec, rate))
        try:
            stream = await asyncio.shield(opening)
        except asyncio.CancelledError:
            try:
                stream = await opening
            except Exception:
                pass
            else:
                await stream.close()
            raise
        self.transports = [s for s in self.transports if not s.closed]
        self.transports.append(stream)
        return stream

    async def set_volume(self, value):
        if self.spec["kind"] != "speaker" or type(value) is not int or not 0 <= value <= 100:
            raise ValueError("Speaker volume must be 0..100")
        target = max(value, self.minimum_volume) if value else 0
        actual, muted = await asyncio.to_thread(volume, self.spec["connection"]["endpoint_id"], target)
        self.volume = actual
        return {"volume_percent": actual, "muted": muted, "applied": True, "scope": "device"}

    async def inspect(self):
        try:
            await self.ensure_ready()
        except Exception:
            for stream in self.transports:
                stream.error = "Configured audio endpoint disconnected"
            raise
        result = {"stream_open": any(s.healthy for s in self.transports)}
        if self.spec["kind"] == "speaker":
            self.volume, muted = await asyncio.to_thread(volume, self.spec["connection"]["endpoint_id"])
            result.update(volume_percent=self.volume, muted=muted)
        return result

    async def close(self):
        # Stream registries own transport cleanup and leases.
        for stream in self.transports:
            stream.error = "Audio provider stopped"
