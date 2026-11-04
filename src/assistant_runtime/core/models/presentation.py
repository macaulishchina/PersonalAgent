"""Public model appearance and immutable, configuration-owned image resources."""

import base64
import hashlib
from io import BytesIO
from pathlib import Path
import re
import warnings

BUILTINS = ("generic", "openai", "anthropic", "google", "deepseek")
MAX_UPLOAD = 2 * 1024 * 1024
MAX_IMAGE = 256 * 1024


def validate(value):
    if not isinstance(value, dict) or set(value) - {"display_name", "description", "avatar"}:
        raise ValueError("Invalid model presentation")
    for key, bound in (("display_name", 80), ("description", 400)):
        if key in value and (
            not isinstance(value[key], str)
            or len(value[key]) > bound
            or key == "display_name"
            and not value[key].strip()
        ):
            raise ValueError("Invalid public model text")
    avatar = value.get("avatar", "builtin:generic")
    if not isinstance(avatar, str) or not (
        avatar in {"builtin:" + v for v in BUILTINS} or re.fullmatch(r"asset:[a-f0-9]{64}", avatar)
    ):
        raise ValueError("Invalid model avatar reference")


def normalize(raw):
    from PIL import Image, ImageOps

    if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_UPLOAD:
        raise ValueError("Avatar upload exceeds 2 MiB")
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with Image.open(BytesIO(raw)) as image:
            if image.format not in {"PNG", "JPEG", "WEBP"} or image.width * image.height > 16_000_000:
                raise ValueError("Unsupported avatar image")
            image.seek(0)
            image = ImageOps.exif_transpose(image).convert("RGBA")
            image.thumbnail((256, 256))
            clean = Image.new("RGBA", image.size)
            clean.paste(image)
            out = BytesIO()
            clean.save(out, format="PNG", optimize=True)
            result = out.getvalue()
    if len(result) > MAX_IMAGE:
        raise ValueError("Normalized avatar exceeds 256 KiB")
    return result


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def references(data):
    registry = data.get("model_api", {})
    return {
        p["presentation"]["avatar"][6:]
        for group in ("providers", "entries")
        for p in registry.get(group, {}).values()
        if isinstance(p.get("presentation", {}).get("avatar"), str)
        and re.fullmatch(r"asset:[a-f0-9]{64}", p["presentation"]["avatar"])
    }


def registered(root, data):
    from assistant_runtime.api.configuration import ConfigurationResource

    return [
        ConfigurationResource(
            "model-avatar-" + ident,
            Path(root) / "config/model-avatars" / (ident + ".png"),
            "model_avatar",
            "模型头像",
        )
        for ident in sorted(references(data))
    ]


def validate_image(ident, raw):
    if raw is None or len(raw) > MAX_IMAGE or digest(raw) != ident or normalize(raw) != raw:
        raise ValueError("Invalid or missing normalized model avatar")


def read(root, ident):
    from assistant_runtime.api.configuration_io import configuration_path

    if not isinstance(ident, str) or not re.fullmatch(r"[a-f0-9]{64}", ident):
        raise ValueError("Invalid avatar identity")
    path = Path(root) / "config/model-avatars" / (ident + ".png")
    path = configuration_path(path)
    if path.is_symlink() or any(p.is_symlink() for p in path.parents):
        raise ValueError("Invalid avatar resource")
    with path.open("rb") as stream:
        raw = stream.read(MAX_IMAGE + 1)
    validate_image(ident, raw)
    return raw


def public(settings, model):
    entry = settings.entries[model]
    provider = settings.providers[entry["provider"]]
    p, e = provider.get("presentation", {}), entry.get("presentation", {})
    avatar = e.get("avatar", p.get("avatar", "builtin:generic"))
    return {
        "display_name": e.get("display_name", model),
        "description": e.get("description", ""),
        "provider_display_name": p.get("display_name", entry["provider"]),
        "provider_description": p.get("description", ""),
        "avatar": {"kind": "builtin", "name": avatar[8:]}
        if avatar.startswith("builtin:")
        else {
            "kind": "image",
            "asset_id": avatar[6:],
            "version": avatar[6:],
            "media_type": "image/png",
            "sha256": avatar[6:],
        },
    }


def encoded(root, ident):
    raw = read(root, ident)
    return {
        "asset_id": ident,
        "version": ident,
        "media_type": "image/png",
        "sha256": ident,
        "data_base64": base64.b64encode(raw).decode(),
    }
