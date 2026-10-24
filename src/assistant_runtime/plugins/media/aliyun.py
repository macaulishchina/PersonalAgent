"""Account-scoped single-writer auth and owned, deadline-bound SDK subprocesses."""

import asyncio
from assistant_runtime.api.system import system
import hashlib
import json
from pathlib import Path
import shutil
import sys
import time
import uuid

from .errors import MediaError
from .private import disk, remove, save

WRITES = {"copy", "mkdir", "rename", "move", "trash", "restore"}


class Aliyun:
    def __init__(self, root, account_changed, *, account="aliyun"):
        self.root, self.account_changed = Path(root).resolve(), account_changed
        self.credentials = self.root / "credentials" / account
        self.credentials.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lease = None
        self.lock = asyncio.Lock()
        self.children = set()
        self.login = None
        self.closed = False
        self.barrier = self.credentials / "auth-blocked.json"
        try:
            blocked = json.loads(self.barrier.read_text(encoding="utf-8")) if self.barrier.exists() else None
            # A changed atomic credential file proves a save completed before a
            # crash. An unchanged file must never retry an uncertain rotation.
            self.auth_required = self.barrier.exists() and (
                not isinstance(blocked, dict) or blocked["credential_stamp"] == self.credential_stamp()
            )
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            self.auth_required = True

    def credential_stamp(self):
        path = self.credentials / "account.json"
        if not path.exists():
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
        tokens = {key: value.get(key) for key in ("access_token", "refresh_token")}
        return hashlib.sha256(json.dumps(tokens, sort_keys=True).encode()).hexdigest()

    async def block_auth(self):
        self.auth_required = True
        try:
            await disk(save, self.barrier, {"credential_stamp": self.credential_stamp()})
        except (OSError, ValueError, AttributeError):
            raise MediaError("auth_required") from None

    async def clear_auth(self):
        try:
            if self.barrier.exists():
                await disk(remove, self.barrier)
        except OSError:
            self.auth_required = True
            raise MediaError("auth_required") from None
        self.auth_required = False

    def claim(self):
        if self.closed:
            raise MediaError("media_stopping")
        if self.lease is not None:
            return
        lease = (self.credentials / "owner.lock").open("a")
        system.private(lease.name)
        try:
            system.lock(lease, blocking=False)
        except OSError:
            lease.close()
            raise MediaError("aliyun_credentials_already_owned") from None
        self.lease = lease

    def binding(self):
        path = self.credentials / "identity.json"
        return json.loads(path.read_text(encoding="utf-8"))["identity"] if path.exists() else None

    async def run(self, action, arguments=None, *, directory=None, on_qr=None):
        self.claim()
        if self.closed:
            raise MediaError("media_stopping")
        process = await system.spawn(
            sys.executable,
            "-m",
            "assistant_runtime.plugins.media.aliyun_worker",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            limit=3 * 1024 * 1024,
        )
        self.children.add(process)
        try:
            if self.closed:
                raise MediaError("media_stopping")
            async with asyncio.timeout(205 if action == "login" else 35):
                message = {
                    "action": action,
                    "arguments": arguments or {},
                    "directory": str(directory or self.credentials),
                    "expected_identity": self.binding(),
                }
                process.stdin.write((json.dumps(message) + "\n").encode())
                await process.stdin.drain()
                process.stdin.close()
                for _ in range(3):
                    line = await process.stdout.readline()
                    if not line:
                        raise MediaError("sdk_worker_disconnected", uncertain=action in WRITES)
                    try:
                        result = json.loads(line)
                    except ValueError:
                        raise MediaError("sdk_worker_protocol_error", uncertain=action in WRITES) from None
                    if "qr_base64" in result and on_qr:
                        on_qr(result["qr_base64"])
                        continue
                    if "error" in result:
                        raise MediaError(result["error"], uncertain=bool(result.get("uncertain")))
                    if "result" not in result:
                        raise MediaError("sdk_worker_protocol_error", uncertain=action in WRITES)
                    await process.wait()
                    return result["result"]
                raise MediaError("sdk_worker_protocol_error", uncertain=action in WRITES)
        except (TimeoutError, OSError, ValueError):
            raise MediaError("sdk_worker_timeout", uncertain=action in WRITES) from None
        finally:
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
            await process.wait()
            self.children.discard(process)

    async def call(self, action, arguments=None):
        async with self.lock:
            if self.auth_required:
                self.account_changed("auth_required")
                raise MediaError("auth_required")
            try:
                repaired = set()
                while True:
                    try:
                        result = await self.run(action, arguments)
                        break
                    except MediaError as exc:
                        repair = {"access_expired": "refresh", "device_session_expired": "session"}.get(
                            exc.code
                        )
                        if repair is None or repair in repaired:
                            raise
                        repaired.add(repair)
                        # Only explicit pre-execution rejections allow another call.
                        # Network timeouts and uncertain writes never enter here.
                        try:
                            if repair == "refresh":
                                await self.block_auth()
                            await self.run(repair)
                            if repair == "refresh":
                                await self.clear_auth()
                        except MediaError:
                            if repair == "refresh":
                                raise MediaError("auth_required") from None
                            raise
                if action == "status" and not self.binding():
                    await disk(save, self.credentials / "identity.json", {"identity": result["identity"]})
                await self.clear_auth()
                self.account_changed("ready")
                return result
            except MediaError as exc:
                if exc.code in {"auth_required", "access_expired", "account_mismatch"}:
                    await self.block_auth()
                    self.account_changed("auth_required")
                    raise MediaError("auth_required") from None
                raise

    def begin_login(self, context, completed):
        if self.login and self.login["state"] in {"starting", "scan_required"}:
            return self.login
        session = {"login_id": uuid.uuid4().hex, "state": "starting", "expires_at": time.time() + 180}
        self.login = session
        context.spawn(self.login_worker(session, completed), "aliyun-login")
        return session

    async def login_worker(self, session, completed):
        pending = self.root / "login" / session["login_id"]

        def qr(image):
            if len(image) > 256 * 1024:
                raise MediaError("login_image_limit")
            session.update(state="scan_required", qr_base64=image)

        try:
            result = await self.run("login", directory=pending, on_qr=qr)
            async with self.lock:
                if self.binding() and self.binding() != result["identity"]:
                    raise MediaError("account_mismatch")
                token = await disk(lambda: json.loads((pending / "account.json").read_text(encoding="utf-8")))
                await disk(save, self.credentials / "identity.json", {"identity": result["identity"]})
                await disk(save, self.credentials / "account.json", token)
                await self.clear_auth()
                self.account_changed("ready")
                completed()
            session["state"] = "completed"
        except asyncio.CancelledError:
            session["state"] = "cancelled"
            raise
        except MediaError as exc:
            session.update(
                state="expired" if time.time() >= session["expires_at"] else "failed", error=exc.code
            )
        except Exception:
            session.update(state="failed", error="login_storage_failed")
        finally:
            session.pop("qr_base64", None)
            await disk(shutil.rmtree, pending, True)

    async def close(self):
        self.closed = True
        for process in tuple(self.children):
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
        await asyncio.gather(*(p.wait() for p in tuple(self.children)), return_exceptions=True)
        if self.lease is not None:
            self.lease.close()
