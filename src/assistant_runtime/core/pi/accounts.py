"""Private Pi-native model accounts. No agent loop, global login files or automatic inference."""

import asyncio
import json
import os
from pathlib import Path
import shutil
import sys
import uuid

from assistant_runtime.core.models.gateway import credential


async def installation(binary=None):
    """Resolve the explicitly installed pinned npm runtime; never install on startup."""
    executable = shutil.which(binary or "pi")
    if binary and not executable:
        raise RuntimeError("Configured Pi executable unavailable")
    if executable:
        path = Path(executable).resolve()
        for parent in (path.parent, *path.parents):
            manifest = parent / "package.json"
            if manifest.is_file():
                try:
                    package = json.loads(manifest.read_text())
                except (ValueError, OSError):
                    continue
                if package.get("name") == "@earendil-works/pi-coding-agent":
                    if package.get("version") != "1.4.0":
                        raise RuntimeError("Native accounts require accepted Pi version 1.4.0")
                    return parent
        # A launcher wrapper has no package ancestry. Check its version before
        # using the same-version SDK from the explicitly selected Node/npm installation.
        probe = await asyncio.create_subprocess_exec(executable, "--version", stdout=-1, stderr=-1)
        try:
            output, _ = await asyncio.wait_for(probe.communicate(), 10)
        except BaseException:
            if probe.returncode is None:
                probe.kill()
                await probe.wait()
            raise
        if probe.returncode or output.decode().strip() != "1.4.0":
            raise RuntimeError("Native accounts require accepted Pi version 1.4.0")
    npm = shutil.which("npm")
    if not npm:
        raise RuntimeError("Native model accounts require the pinned npm Pi installation")
    process = await asyncio.create_subprocess_exec(npm, "root", "-g", stdout=-1, stderr=-1)
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), 10)
    except BaseException:
        process.kill()
        await process.wait()
        raise
    root = Path(stdout.decode().strip()) / "@earendil-works/pi-coding-agent"
    if process.returncode or not root.is_dir():
        raise RuntimeError("Pinned Pi package unavailable; run scripts/setup_pi.sh")
    if json.loads((root / "package.json").read_text())["version"] != "1.4.0":
        raise RuntimeError("Native accounts require accepted Pi version 1.4.0")
    return root


class AccountWorker:
    def __init__(self, root, folder, spec, *, binary=None):
        self.root, self.folder, self.spec = Path(root), Path(folder), spec
        self.binary = binary
        self.process = self.reader = None
        self.initialized = False
        self.pending = {}
        self.lock = asyncio.Lock()

    async def start(self):
        async with self.lock:
            if self.process:
                if self.process.returncode is not None or not self.initialized:
                    raise RuntimeError("Native account process exited; explicit restart required")
                return
            package = await installation(self.binary)
            # Provider credentials are supplied privately in init, never inherited from Coding AI.
            environment = {
                k: os.environ[k]
                for k in ("PATH", "HOME", "LANG", "SSL_CERT_FILE", "NODE_EXTRA_CA_CERTS")
                if k in os.environ
            }
            environment.update(PI_TELEMETRY="0", PI_OFFLINE="1", PI_SKIP_VERSION_CHECK="1")
            self.process = await asyncio.create_subprocess_exec(
                shutil.which("node"),
                str(Path(__file__).with_suffix(".mjs")),
                str(package),
                stdin=-1,
                stdout=-1,
                stderr=asyncio.subprocess.DEVNULL,
                env=environment,
                limit=8 * 1024 * 1024,
            )
            self.reader = asyncio.create_task(self._read())
            args = {"folder": str(self.folder), "spec": self.spec}
            if self.spec.get("key_env"):
                args["key"] = credential(self.root, self.spec["key_env"])
            await self.call("init", args)
            self.initialized = True

    async def send(self, data):
        self.process.stdin.write((json.dumps(data) + "\n").encode())
        await self.process.stdin.drain()

    async def _read(self):
        try:
            while raw := await self.process.stdout.readline():
                if len(raw) > 8 * 1024 * 1024:
                    raise ValueError("Native account frame exceeds bound")
                data = json.loads(raw)
                queue = self.pending.get(data.get("id"))
                if queue is not None:
                    queue.put_nowait(data)
                await asyncio.sleep(0)
        except (ValueError, asyncio.QueueFull):
            if self.process.returncode is None:
                self.process.terminate()
        finally:
            for queue in self.pending.values():
                # A stalled consumer cannot block cleanup or another caller.
                if queue.full():
                    queue.get_nowait()
                queue.put_nowait({"error": "native_connection_lost"})

    async def events(self, action, arguments=None):
        if len(self.pending) >= 16:
            raise RuntimeError("Native account queue full")
        ident = uuid.uuid4().hex
        queue = asyncio.Queue(maxsize=128)
        self.pending[ident] = queue
        terminal = False
        try:
            await self.send({"id": ident, "action": action, "arguments": arguments or {}})
            while True:
                data = await queue.get()
                if "error" in data:
                    terminal = True
                    raise RuntimeError(data["error"])
                yield data
                if "result" in data:
                    terminal = True
                    return
        finally:
            self.pending.pop(ident, None)
            if not terminal and self.process.returncode is None:
                await self.send({"action": "cancel", "target": ident})

    async def call(self, action, arguments=None):
        async with asyncio.timeout(15):
            async for item in self.events(action, arguments):
                if "result" in item:
                    return item["result"]

    async def close(self):
        if self.process:
            if self.process.returncode is None:
                self.process.terminate()
                try:
                    await asyncio.wait_for(self.process.wait(), 3)
                except TimeoutError:
                    self.process.kill()
                    await self.process.wait()
            await asyncio.gather(self.reader, return_exceptions=True)


class NativeAccounts:
    def __init__(self, root, settings, *, state_dir=None, binary=None):
        self.root, self.settings = Path(root), settings
        self.binary = binary
        self.folder = Path(state_dir) if state_dir else self.root / "state/model-accounts"
        self.workers = {}

    async def account(self, name):
        if name not in self.settings.accounts:
            raise ValueError("Unknown configured model account")
        if name not in self.workers:
            self.workers[name] = AccountWorker(
                self.root, self.folder / name, self.settings.accounts[name], binary=self.binary
            )
        worker = self.workers[name]
        await worker.start()
        return worker

    async def route(self, alias, effort, *, images=False, schema=None):
        entry = self.settings.entry(alias)
        provider = self.settings.providers[entry["provider"]]
        worker = await self.account(provider["account"])
        args = {"model": entry["model"], "alias": alias, "effort": effort, "images": images, "schema": schema}
        validation = await worker.call("validate", args)
        bound = validation["max_output_tokens"] if worker.spec.get("output_limit") == "model" else None
        return worker, args, bound

    async def close(self):
        await asyncio.gather(*(w.close() for w in self.workers.values()))
        self.workers.clear()


async def manage(root, settings, action, account=None, *, binary=None):
    """Explicit operator-only login. Auth prompts never go through model tools or logs."""
    accounts = NativeAccounts(root, settings, binary=binary)
    try:
        if action == "providers":
            worker = AccountWorker(root, accounts.folder, {})
            worker.process = await asyncio.create_subprocess_exec(
                shutil.which("node"),
                str(Path(__file__).with_suffix(".mjs")),
                str(await installation(binary)),
                stdin=-1,
                stdout=-1,
                stderr=asyncio.subprocess.DEVNULL,
                limit=8 * 1024 * 1024,
                env={"PATH": os.environ["PATH"], "PI_TELEMETRY": "0"},
            )
            worker.reader = asyncio.create_task(worker._read())
            try:
                return await worker.call("providers")
            finally:
                await worker.close()
        worker = await accounts.account(account)
        if action != "login":
            return await worker.call(action)
        prompt_task = None

        async def answer(event, call_id):
            prompt = event["data"]
            print(json.dumps(prompt, ensure_ascii=False), flush=True)
            try:
                value = await terminal_input(secret=prompt["type"] in {"secret", "manual_code"})
            except (OSError, EOFError):
                await worker.send({"action": "cancel", "target": call_id})
                return
            await worker.send({"action": "answer", "prompt_id": event["prompt_id"], "value": value})

        try:
            async with asyncio.timeout(300):
                async for item in worker.events("login"):
                    if "result" in item:
                        return item["result"]
                    event = item["event"]
                    if event["type"] == "auth":
                        print(json.dumps(event["data"], ensure_ascii=False), flush=True)
                    elif event["type"] == "prompt":
                        if prompt_task:
                            prompt_task.cancel()
                            await asyncio.gather(prompt_task, return_exceptions=True)
                        prompt_task = asyncio.create_task(answer(event, item["id"]))
        except TimeoutError:
            raise RuntimeError("Native account login timed out; run model-accounts login again") from None
        finally:
            if prompt_task:
                prompt_task.cancel()
                await asyncio.gather(prompt_task, return_exceptions=True)
    finally:
        await accounts.close()


async def terminal_input(*, secret):
    """Cancellable input: browser completion must not leave a blocked input thread."""
    import termios

    loop = asyncio.get_running_loop()
    future = loop.create_future()
    fd = sys.stdin.fileno()
    previous = termios.tcgetattr(fd) if secret and os.isatty(fd) else None
    if previous:
        hidden = list(previous)
        hidden[3] &= ~termios.ECHO
        termios.tcsetattr(fd, termios.TCSANOW, hidden)

    def ready():
        if not future.done():
            value = sys.stdin.readline()
            if not value:
                future.set_exception(EOFError("Model login input closed"))
            else:
                future.set_result(value.rstrip("\n"))

    try:
        loop.add_reader(fd, ready)
        return await future
    finally:
        loop.remove_reader(fd)
        if previous:
            termios.tcsetattr(fd, termios.TCSANOW, previous)
            print(flush=True)
