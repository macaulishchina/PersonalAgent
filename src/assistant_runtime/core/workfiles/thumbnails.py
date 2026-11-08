"""Small RGB565 publications for constrained displays; no URL fetching."""

from array import array
import hashlib
import io
import sys


def render(data, width, height):
    from PIL import Image, ImageOps

    with Image.open(io.BytesIO(data)) as image:
        if image.width * image.height > 20_000_000:
            raise ValueError("Image dimensions exceed bound")
        image = ImageOps.exif_transpose(image)
        image.thumbnail((width, height), Image.Resampling.LANCZOS)
        image = image.convert("RGB")
        rgb = memoryview(image.tobytes())
        pixels = array(
            "H",
            ((rgb[i] >> 3) << 11 | (rgb[i + 1] >> 2) << 5 | (rgb[i + 2] >> 3) for i in range(0, len(rgb), 3)),
        )
        if sys.byteorder != "little":
            pixels.byteswap()
        raw = pixels.tobytes()
        return {
            "width": image.width,
            "height": image.height,
            "format": "rgb565le",
            "version": hashlib.sha256(raw).hexdigest(),
            "data": raw,
            "bytes": len(raw),
        }
