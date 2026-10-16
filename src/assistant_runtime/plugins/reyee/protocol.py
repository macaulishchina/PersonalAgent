"""Read-only device protocol; failures never mean an empty snapshot."""

import asyncio
import base64
import hashlib
import json
import os
import re
import time


def parse_clients(payload):
    if not isinstance(payload, dict):
        raise ValueError("Invalid router response")
    data = payload.get("data")
    if (
        payload.get("code") not in (0, "0")
        or payload.get("error")
        or not isinstance(data, dict)
        or str(data.get("code")) != "0"
    ):
        raise ValueError("Router rejected read or authentication expired")
    rows = data.get("list")
    if not isinstance(rows, list) or len(rows) > 2048 or str(len(rows)) != str(data.get("total")):
        raise ValueError("Incomplete router snapshot")
    result = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("Invalid client")
        mac = str(row.get("mac", "")).lower().replace("-", ":")
        if not re.fullmatch(r"(?:[0-9a-f]{2}:){5}[0-9a-f]{2}", mac) or mac in result:
            raise ValueError("Invalid or duplicate client identity")
        if row.get("connectType") not in {"wire", "wireless"}:
            raise ValueError("Unknown client connection type; snapshot not complete")
        fields = {key: row.get(key, "") for key in ("userIp", "connectType", "sn", "ssid", "band")}
        if any(not isinstance(value, str) or len(value) > 256 for value in fields.values()):
            raise ValueError("Malformed client attributes")
        fields["online_time_evidence"] = str(row.get("onlinetime", ""))[:100]
        result[mac] = fields
    return result


class Reyee:
    def __init__(self, config, directory):
        self.config, self.directory = config, directory
        self.sid = None
        self.session = None

    async def open(self):
        import aiohttp

        self.session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=8), cookie_jar=aiohttp.CookieJar(unsafe=True)
        )

    async def close(self):
        if self.session:
            await self.session.close()

    async def request(self, path, payload=None):
        async with self.session.request(
            "GET" if payload is None else "POST",
            self.config["url"].rstrip("/") + path,
            json=payload,
            allow_redirects=False,
        ) as reply:
            if reply.status != 200:
                raise ValueError("Router HTTP read failed")
            chunks, total = [], 0
            async for chunk in reply.content.iter_chunked(65536):
                total += len(chunk)
                if total > 1024 * 1024:
                    raise ValueError("Router response exceeds limit")
                chunks.append(chunk)
            body = b"".join(chunks)
            if len(body) > 1024 * 1024:
                raise ValueError("Router response exceeds limit")
            return body.decode("utf-8")

    async def login(self):
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        from cryptography.hazmat.primitives.padding import PKCS7

        page = await self.request("/cgi-bin/luci/")
        match = re.search(r'GibberishAES\.enc\(passwordEl\.value,\s*"([^"]+)"', page)
        if not match:
            raise ValueError("Unsupported router login protocol")
        secret_path = self.directory / "credentials" / self.config["credential"]
        if secret_path.is_symlink():
            raise ValueError("Credential reference cannot be a symlink")
        if secret_path.stat().st_mode & 0o077:
            raise ValueError("Credential file must be private (0600)")
        if secret_path.stat().st_size > 4096:
            raise ValueError("Credential file exceeds limit")
        password = await asyncio.to_thread(secret_path.read_text)
        salt, material, block = os.urandom(8), b"", b""
        while len(material) < 48:
            block = hashlib.md5(block + match[1].encode() + salt).digest()
            material += block
        pad = PKCS7(128).padder()
        plain = pad.update(password.rstrip("\n").encode()) + pad.finalize()
        encryptor = Cipher(algorithms.AES(material[:32]), modes.CBC(material[32:48])).encryptor()
        encrypted = base64.b64encode(
            b"Salted__" + salt + encryptor.update(plain) + encryptor.finalize()
        ).decode()
        reply = json.loads(
            await self.request(
                "/cgi-bin/luci/api/auth",
                {
                    "method": "login",
                    "params": {
                        "username": "admin",
                        "time": str(round(time.time())),
                        "encry": True,
                        "pwd": encrypted,
                    },
                },
            )
        )
        sid = reply.get("data", {}).get("sid")
        if not isinstance(sid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", sid):
            raise ValueError("Router login failed")
        self.sid = sid

    async def sample(self):
        try:
            if self.sid is None:
                await self.login()
            payload = json.loads(
                await self.request(
                    "/cgi-bin/luci/api/cmd?auth=" + self.sid,
                    {
                        "method": "devSta.get",
                        "params": {
                            "module": "user_list",
                            "data": {"devType": "all", "dataType": "timely"},
                            "noParse": True,
                            "device": "pc",
                        },
                    },
                )
            )
            return parse_clients(payload)
        except Exception:
            self.sid = None
            # Never propagate URLs (sid query) or response bodies to status/logs.
            raise RuntimeError(
                "Router snapshot unavailable; authentication, connectivity or format failed"
            ) from None
