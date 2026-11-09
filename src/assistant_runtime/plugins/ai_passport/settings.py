"""Canonical file settings and per-device desired values, without device I/O."""

import re
from urllib.parse import urlsplit

from assistant_runtime.api.configuration import ConfigurationError


LOCAL_FIELDS = ("volume", "input_mode", "keep_online")
DEVICE_FIELDS = (
    *LOCAL_FIELDS,
    "sleep_seconds",
    "speech_mode",
    "offline_cache",
    "show_images",
    "brightness",
    "text_size",
)
NETWORK_FIELDS = ("wifi_networks", "server_url", "trusted_ca")


def desired(settings, device):
    values = {k: settings[k] for k in (*DEVICE_FIELDS, "voice", "rate") if k in settings}
    values.update(settings.get("devices", {}).get(device, {}))
    if "wifi_networks" in values:
        networks = values["wifi_networks"]
        values["wifi_networks"] = [networks[k] for k in sorted(networks, key=int)]
    return values


def validate(settings):
    def reject(device, field, message):
        raise ConfigurationError(
            ["plugin_settings", "ai_passport", "devices", device, field], "value", message
        )

    for device, values in settings.get("devices", {}).items():
        seen = set()
        for network in values.get("wifi_networks", {}).values():
            ssid, password = network["ssid"], network["password"]
            if not 1 <= len(ssid.encode()) <= 32 or "\x00" in ssid or ssid in seen:
                reject(device, "wifi_networks", "Wi-Fi 名称须为 1—32 字节且不能重复")
            seen.add(ssid)
            if (
                "\x00" in password
                or password
                and not (8 <= len(password.encode()) <= 63 or re.fullmatch(r"[0-9a-fA-F]{64}", password))
            ):
                reject(device, "wifi_networks", "Wi-Fi 密码须为 8—63 字节或 64 位十六进制，开放网络留空")
        if "server_url" in values:
            raw = values["server_url"]
            try:
                url = urlsplit(raw)
                valid = (
                    url.scheme in {"http", "https"}
                    and url.hostname
                    and re.fullmatch(r"[A-Za-z0-9.:-]+", url.hostname)
                    and url.port != 0
                    and not url.username
                    and not url.password
                    and not url.query
                    and not url.fragment
                    and url.path in {"", "/"}
                    and not any(c.isspace() or ord(c) < 32 for c in raw)
                )
            except ValueError:
                valid = False
            if not valid:
                reject(device, "server_url", "请填写 HTTP 或 HTTPS 业务根地址，不含账号、路径或查询参数")
        ca = values.get("trusted_ca", "")
        if ca and ("-----BEGIN CERTIFICATE-----" not in ca or "PRIVATE KEY" in ca):
            reject(device, "trusted_ca", "只能填写 CA 公钥证书，不能填写私钥")
    return settings
