"""Opt-in ALSA mixer initialization, scoped to the resolved PipeWire endpoint."""

import re


def hardware_volume(connection):
    if "hardware_volume" not in connection:
        return None
    value = connection["hardware_volume"]
    if not isinstance(value, dict) or set(value) - {"control", "index", "percent"}:
        raise ValueError("hardware_volume accepts control, index and percent")
    control, index, percent = value.get("control"), value.get("index", 0), value.get("percent")
    if (
        not isinstance(control, str)
        or not re.fullmatch(r"[\w .:/()+-]{1,100}", control)
        or control != control.strip()
        or type(index) is not int
        or not 0 <= index <= 255
        or type(percent) is not int
        or not 0 <= percent <= 100
    ):
        raise ValueError("Invalid hardware_volume control/index/percent (0..100)")
    return {"control": control, "index": index, "percent": percent}


def _read_control(output):
    meta = re.search(r"type=INTEGER,access=([^,\s]+),values=(\d+),min=(-?\d+),max=(-?\d+),step=(\d+)", output)
    values = re.search(r"^\s*: values=([-\d,]+)\s*$", output, re.MULTILINE)
    if meta is None or values is None or not {"r", "w"} <= set(meta[1]):
        raise RuntimeError("Hardware volume must be a readable, writable integer mixer control")
    count, low, high, step = map(int, meta.groups()[1:])
    actual = [int(value) for value in values[1].split(",")]
    if not 1 <= count <= 32 or len(actual) != count or low >= high:
        raise RuntimeError("Invalid hardware mixer range or channel count")
    return low, high, max(1, step), actual


async def apply_hardware_volume(spec, node):
    settings = hardware_volume(spec["connection"])
    if settings is None:
        return
    from .audio import command

    properties = (node or {}).get("properties", {})
    card = properties.get("alsa.id", properties.get("alsa.card"))
    if (
        isinstance(card, bool)
        or not isinstance(card, (str, int))
        or not re.fullmatch(r"[\w-]{1,64}", str(card))
    ):
        raise RuntimeError("Configured hardware_volume requires an ALSA-backed PipeWire endpoint")
    selector = f"iface=MIXER,name='{settings['control']}',index={settings['index']}"
    try:
        before = await command("amixer", "-c", str(card), "cget", selector, timeout=1)
        low, high, step, previous = _read_control(before)
        after = await command(
            "amixer", "-c", str(card), "cset", selector, f"{settings['percent']}%", timeout=1
        )
        actual_low, actual_high, actual_step, actual = _read_control(after)
        target = low + (high - low) * settings["percent"] / 100
        tolerance = 0 if settings["percent"] in (0, 100) else step
        if (actual_low, actual_high, actual_step, len(actual)) != (low, high, step, len(previous)) or any(
            abs(value - target) > tolerance for value in actual
        ):
            raise RuntimeError("Hardware mixer did not apply the configured volume")
    except (OSError, TimeoutError, RuntimeError, ValueError) as exc:
        raise RuntimeError("Hardware volume initialization failed: " + str(exc)) from exc
