"""Read-only HIDAPI transport for Windows; exact identity, no keyboard hooks."""

import asyncio


def open_device(button):
    import hid

    matches = [
        row
        for row in hid.enumerate(button.vendor_id, button.product_id)
        if row.get("serial_number") == button.serial
    ]
    if len(matches) != 1:
        raise OSError("HID device absent or identity ambiguous")
    selected = matches[0]
    device = hid.device()
    try:
        device.open_path(selected["path"])
        if device.get_serial_number_string() != button.serial:
            raise OSError("HID identity changed while opening")
        device.set_nonblocking(1)
        return device
    except BaseException:
        device.close()
        raise


async def read_reports(button, report, ready):
    opening = asyncio.create_task(asyncio.to_thread(open_device, button))
    try:
        device = await asyncio.shield(opening)
    except asyncio.CancelledError:
        # A cancelled to_thread call still runs. Wait for it before releasing ownership.
        try:
            device = await opening
        except Exception:
            pass
        else:
            await asyncio.to_thread(device.close)
        raise
    try:
        ready()
        while True:
            for _ in range(8):
                packet = device.read(4096)
                if not packet:
                    break
                report(button, bytes(packet))
            await asyncio.sleep(0.02)
    finally:
        await asyncio.to_thread(device.close)
