"""Own only this instance's business process; no model, device or shell operations."""

from assistant_runtime.platforms.ipc import connect

import asyncio
import base64
from assistant_runtime.api.system import system
import json
import os
import sys
import time
import uuid

from assistant_runtime.core.configuration.files import Snapshot, atomic


class BusinessProcess:
    def __init__(self, settings):
        self.settings = settings
        self.state = settings.root / "state/configuration"
        self.state.mkdir(parents=True, exist_ok=True, mode=0o700)
        from assistant_runtime.core.configuration.service import ConfigurationService

        self.configuration = ConfigurationService(settings)
        self.resources, self.files = self.configuration.resources, self.configuration.files
        self.process = None
        self.loaded = None
        self.loaded_snapshot = None
        saved = self.state / "loaded-snapshot.json"
        if saved.exists():
            try:
                data = json.loads(saved.read_text(encoding="utf-8"))
                self.loaded_snapshot = Snapshot(
                    data["version"],
                    {k: base64.b64decode(v, validate=True) for k, v in data["contents"].items()},
                    {
                        k: base64.b64decode(v, validate=True) if v is not None else None
                        for k, v in data["resources"].items()
                    },
                )
                self.loaded = self.loaded_snapshot.version
            except (ValueError, KeyError, TypeError, OSError):
                self.loaded_snapshot = None
        self.outcome = "stopped"
        self.error = None
        self.operation = asyncio.Lock()
        self.logfile = None
        self.apply_task = None
        receipt = self.state / "application.json"
        try:
            self.application = (
                json.loads(receipt.read_text(encoding="utf-8")) if receipt.exists() else {"phase": "idle"}
            )
            if not isinstance(self.application, dict) or not isinstance(self.application.get("phase"), str):
                raise ValueError("Invalid application receipt")
        except (ValueError, OSError):
            self.application = {"phase": "failed", "diagnostic": "application_receipt_invalid"}
        self.reconcile_update()
        if self.application.get("phase") in {
            "preparing",
            "applying",
            "recovering",
            "waiting",
            "cancelling",
            "restarting",
        }:
            self.application.update(phase="failed", interrupted=True)
            self.persist_application()

    def reconcile_update(self):
        if self.application.get("hot") and self.application.get("phase") in {
            "preparing",
            "applying",
            "waiting",
            "failed",
        }:
            try:
                worker = json.loads((self.state / "hot-receipt.json").read_text(encoding="utf-8"))
                pending = json.loads((self.state / "pending-snapshot.json").read_text(encoding="utf-8"))
                if (
                    worker.get("phase") == "applied"
                    and (
                        not self.application.get("attempt_id")
                        or worker.get("attempt_id") == self.application["attempt_id"]
                    )
                    and worker.get("version") == pending["version"] == self.application.get("version")
                ):
                    self.accept_snapshot(
                        Snapshot(
                            pending["version"],
                            {k: base64.b64decode(v, validate=True) for k, v in pending["contents"].items()},
                            {
                                k: base64.b64decode(v, validate=True) if v is not None else None
                                for k, v in pending["resources"].items()
                            },
                        )
                    )
                    self.application.update(worker)
                    if (
                        self.application.get("required_steps")
                        or self.application.get("management_pending")
                        or worker.get("cleanup_failed")
                    ):
                        self.application["phase"] = "partially_applied"
                    self.persist_application()
            except (OSError, ValueError, KeyError, TypeError):
                pass

    def persist_application(self):
        atomic(self.state / "application.json", json.dumps(self.application).encode())
        if self.application.get("phase") == "applied" and self.application.get("version"):
            atomic(
                self.state / "confirmed.json", json.dumps({"version": self.application["version"]}).encode()
            )

    def application_phase(self, phase):
        self.application["phase"] = phase
        self.persist_application()
        self.files._audit(
            {
                "time": time.time(),
                "actor": "administrator",
                "result": phase,
                "version": self.application.get("version"),
            }
        )

    def complete_step(self, version, component, confirmed):
        if (
            confirmed is not True
            or version != self.application.get("version")
            or version != self.loaded
            or self.application.get("phase") != "partially_applied"
        ):
            raise ValueError("Explicit confirmation of the applied version is required")
        steps = self.application.get("required_steps", [])
        step = next((step for step in steps if step["component"] == component), None)
        if step is None:
            raise ValueError("Unknown outstanding deployment step")
        self.application["required_steps"] = [s for s in steps if s is not step]
        self.application.setdefault("operator_confirmed_steps", []).append(component)
        self.application_phase(
            "partially_applied"
            if self.application["required_steps"]
            or self.application.get("management_pending")
            or self.application.get("cleanup_failed")
            or self.outcome == "degraded"
            else "applied"
        )
        return dict(self.application)

    async def status(self):
        self.reconcile_update()
        running = self.process is not None and self.process.returncode is None
        result = {
            "state": self.outcome if running or self.error else "stopped",
            "loaded_version": self.loaded,
            "error": self.error,
            "pid": self.process.pid if running else None,
            "application": self.application,
        }
        if running:
            try:
                result["business"] = await self.request({"command": "status"})
            except (OSError, ValueError, TimeoutError):
                result["business"] = {"state": "unavailable"}
            business = result["business"]
            result["plugin_issues"] = [
                {
                    "id": name,
                    "state": item.get("state"),
                    "error": item.get("error") or "插件未就绪，请检查业务日志。",
                }
                for name, item in business.get("plugins", {}).items()
                if item.get("state") in {"failed", "degraded", "blocked"} or item.get("error")
            ]
            if business.get("state") == "unavailable":
                result["plugin_issues"].append(
                    {
                        "id": "business",
                        "state": "unavailable",
                        "error": "无法读取业务状态；请检查业务进程和 business.log。",
                    }
                )
            if self.outcome in {"running", "degraded"}:
                result["state"] = "degraded" if result["plugin_issues"] else "running"
        return result

    async def request(self, payload):
        async with asyncio.timeout(
            max(120, self.settings.startup_seconds)
            if payload.get("command") == "configuration-update"
            else 3690
            if payload.get("command") == "mcp" and payload.get("action") == "verify_draft"
            else 10
        ):
            reader, writer = await connect(self.settings.root / "state/control.sock")
            try:
                writer.write(json.dumps(payload).encode() + b"\n")
                await writer.drain()
                reply = json.loads(await reader.readline())
                if not reply.get("ok"):
                    if reply.get("issue"):
                        from assistant_runtime.api.configuration import ConfigurationError

                        issue = reply["issue"]
                        raise ConfigurationError(issue["path"], issue["code"], issue["message"])
                    raise ValueError("Business control request failed")
                return reply["result"]
            finally:
                writer.close()
                await writer.wait_closed()

    def freeze(self, snapshot):
        import tomlkit

        # Workers read a frozen generation. Later saves cannot affect startup or
        # lazy configuration reads; original include names are preserved.
        folder = self.state / "generations" / snapshot.version
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        system.private(folder.parent, directory=True)
        for name, content in snapshot.contents.items():
            if name == self.settings.entry.name:
                document = tomlkit.parse(content.decode())
                document["root"] = str(self.settings.root)
                content = tomlkit.dumps(document).encode()
            atomic(folder / name, content)
        resource_map = {}
        for ident, raw in snapshot.resources.items():
            resource = self.resources.registered(snapshot.contents)[ident]
            target = self.resources.frozen_target(folder, resource)
            if raw is not None:
                atomic(target, raw)
            else:
                target.unlink(missing_ok=True)
            resource_map[str(resource.path.absolute())] = str(target)
        atomic(folder / "resources.json", json.dumps(resource_map).encode())
        return folder / self.settings.entry.name

    async def start(self, snapshot=None):
        async with self.operation:
            if self.process and self.process.returncode is None:
                raise ValueError("Business process is already running")
            try:
                with self.files.locked():
                    if snapshot is None:
                        if (self.state / "blocked-update.json").exists():
                            snapshot = self.loaded_snapshot
                            if snapshot is None:
                                raise ValueError(
                                    "An unconfirmed update requires explicit configuration repair/apply"
                                )
                        else:
                            initial = self.files.snapshot()
                            self.resources.provision(initial.contents)
                    snapshot = snapshot or self.files.snapshot()
                    required_steps = self.application_steps(snapshot)
                    self.files.validate_all(snapshot.contents, snapshot.resources)
                    revision = snapshot.version
                    frozen_entry = self.freeze(snapshot)
                self.outcome, self.error = "starting", None
                logfile = self.state / "business.log"
                self.logfile = logfile.open("ab")
                system.private(logfile)
                env = {
                    **os.environ,
                    "PERSONAL_AGENT_CONFIGURATION_VERSION": revision,
                    "PERSONAL_AGENT_INSTANCE_ROOT": str(self.settings.root),
                    "PERSONAL_AGENT_RESOURCE_MANIFEST": str(frozen_entry.parent / "resources.json"),
                    "PERSONAL_AGENT_OWNED_BUSINESS": "1",
                }
                # Durable ownership also survives termination of the entire Windows
                # job, when no child can write a last-moment interruption receipt.
                marker = self.state / "business-owner.json"
                if marker.exists():
                    atomic(self.state / "unclean.json", b'{"reason":"previous_owner_unconfirmed"}')
                atomic(marker, json.dumps({"version": revision}).encode())
                self.process = await system.spawn(
                    *system.supervised_command(
                        self.settings.shutdown_seconds,
                        [
                            sys.executable,
                            "-m",
                            "assistant_runtime.app.cli",
                            "--config",
                            str(frozen_entry),
                            "run",
                        ],
                    ),
                    stdin=asyncio.subprocess.PIPE,
                    stdout=self.logfile,
                    stderr=self.logfile,
                    start_new_session=True,
                    env=env,
                )
                deadline = time.monotonic() + self.settings.startup_seconds
                while time.monotonic() < deadline:
                    if self.process.returncode is not None:
                        raise RuntimeError("Business exited during startup; inspect private business.log")
                    try:
                        status = await self.request({"command": "management-status"})
                        health = status.get("plugins", {})
                        if status.get("startup_complete"):
                            if (
                                status.get("supervisor_pid") != self.process.pid
                                or status.get("configuration_version") != revision
                            ):
                                raise RuntimeError("Business readiness belongs to another generation")
                            if any(
                                health.get(name, {}).get("state") != "ready"
                                for name in status.get("critical", [])
                            ):
                                raise RuntimeError("Critical business capability unavailable")
                            self.outcome = (
                                "degraded"
                                if any(p.get("state") != "ready" for p in health.values())
                                else "running"
                            )
                            self.loaded, self.loaded_snapshot = revision, snapshot
                            atomic(
                                self.state / "loaded-snapshot.json",
                                json.dumps(
                                    {
                                        "version": revision,
                                        "contents": {
                                            k: base64.b64encode(v).decode()
                                            for k, v in snapshot.contents.items()
                                        },
                                        "resources": {
                                            k: base64.b64encode(v).decode() if v is not None else None
                                            for k, v in snapshot.resources.items()
                                        },
                                    }
                                ).encode(),
                            )
                            if self.application.get("phase") not in {
                                "preparing",
                                "waiting",
                                "cancelling",
                                "restarting",
                            }:
                                self.application.update(
                                    version=revision,
                                    phase="partially_applied"
                                    if required_steps or self.outcome == "degraded"
                                    else "applied",
                                    required_steps=required_steps,
                                )
                                self.persist_application()
                            atomic(
                                self.state / "applied.json",
                                json.dumps({"version": self.loaded, "time": time.time()}).encode(),
                            )
                            self.prune_generations()
                            return
                    except (OSError, ValueError, TimeoutError):
                        pass
                    await asyncio.sleep(0.2)
                raise TimeoutError("Business startup readiness timed out")
            except Exception as exc:
                self.error = (
                    type(exc).__name__ + ": configuration or startup failed; inspect local diagnostics"
                )
                self.outcome = "failed"
                await self.stop()
                self.outcome = "failed"

    def prune_generations(self):
        import shutil

        folder = self.state / "generations"
        generations = sorted(
            (p for p in folder.iterdir() if p.is_dir()), key=lambda p: p.stat().st_mtime, reverse=True
        )
        for path in generations[20:]:
            if path.name not in {self.loaded, self.application.get("version")}:
                shutil.rmtree(path)

    def application_steps(self, snapshot):
        return []

    def accept_snapshot(self, snapshot):
        self.loaded, self.loaded_snapshot = snapshot.version, snapshot
        atomic(
            self.state / "loaded-snapshot.json",
            json.dumps(
                {
                    "version": snapshot.version,
                    "contents": {k: base64.b64encode(v).decode() for k, v in snapshot.contents.items()},
                    "resources": {
                        k: base64.b64encode(v).decode() if v is not None else None
                        for k, v in snapshot.resources.items()
                    },
                }
            ).encode(),
        )
        (self.state / "blocked-update.json").unlink(missing_ok=True)

    def update(self, version):
        """Save/apply never implies permission to restart or cancel accepted work."""
        from assistant_runtime.core.configuration.files import Conflict

        if self.operation.locked() or (self.apply_task and not self.apply_task.done()):
            raise Conflict("Configuration application is already active")
        with self.files.locked():
            snapshot = self.files.snapshot()
            if snapshot.version != version:
                raise Conflict("Reload the configuration before applying")
            self.files.validate_all(snapshot.contents, snapshot.resources)
            entry = self.freeze(snapshot)
        self.application = {
            "phase": "preparing",
            "version": version,
            "hot": True,
            "attempt_id": uuid.uuid4().hex,
            "management_pending": self.application.get("management_pending", False),
            "required_steps": self.application_steps(snapshot),
        }
        self.persist_application()
        atomic(self.state / "blocked-update.json", json.dumps({"version": version}).encode())
        atomic(
            self.state / "pending-snapshot.json",
            json.dumps(
                {
                    "version": snapshot.version,
                    "contents": {k: base64.b64encode(v).decode() for k, v in snapshot.contents.items()},
                    "resources": {
                        k: base64.b64encode(v).decode() if v is not None else None
                        for k, v in snapshot.resources.items()
                    },
                }
            ).encode(),
        )
        self.apply_task = asyncio.create_task(self._update(snapshot, entry), name="configuration-update")
        return dict(self.application)

    async def _update(self, snapshot, entry):
        async with self.operation:
            await self._update_serial(snapshot, entry)

    async def _update_serial(self, snapshot, entry):
        before = self.loaded_snapshot.resources if self.loaded_snapshot else {}
        changed = [
            key
            for key in before.keys() | snapshot.resources.keys()
            if before.get(key) != snapshot.resources.get(key)
        ]
        if self.application.get("management_pending") and "management" not in changed:
            changed.append("management")
        try:
            if not self.process or self.process.returncode is not None:
                (self.state / "blocked-update.json").unlink(missing_ok=True)
                self.application_phase("pending_start")
                return
            request = {
                "command": "configuration-update",
                "entry": str(entry),
                "version": snapshot.version,
                "resources": changed,
                "attempt_id": self.application.get("attempt_id"),
            }
            while True:
                try:
                    result = await self.request(request)
                except (OSError, ValueError, TimeoutError):
                    # A lost IPC response must not misreport a completed worker transaction.
                    receipt = self.state / "hot-receipt.json"
                    result = json.loads(receipt.read_text(encoding="utf-8")) if receipt.exists() else {}
                    if (
                        result.get("version") != snapshot.version
                        or result.get("attempt_id") != request["attempt_id"]
                        or result.get("phase")
                        not in {
                            "preparing",
                            "applying",
                            "recovering",
                            "waiting",
                            "applied",
                            "failed",
                            "pending_restart",
                        }
                        or self.process.returncode is not None
                    ):
                        raise
                self.application.update(result)
                self.persist_application()
                if result["phase"] not in {"waiting", "preparing", "applying", "recovering"}:
                    break
                await asyncio.sleep(0.5)
            if result["phase"] == "applied":
                self.accept_snapshot(snapshot)
                if "management" in changed:
                    try:
                        self.application["management_address"] = await self.management_applied()
                        self.application["management_pending"] = False
                    except Exception:
                        # The worker committed, but the independent management listener stayed old.
                        self.application.update(
                            phase="partially_applied",
                            diagnostic="management_listener_update_failed",
                            management_pending=True,
                        )
                if self.application.get("required_steps") or result.get("cleanup_failed"):
                    self.application["phase"] = "partially_applied"
                self.persist_application()
            elif result["phase"] == "pending_restart":
                (self.state / "blocked-update.json").unlink(missing_ok=True)
        except asyncio.CancelledError:
            self.application_phase("failed")
            raise
        except Exception as exc:
            from assistant_runtime.api.configuration import ConfigurationError

            self.application.update(phase="failed", diagnostic="configuration_update_failed")
            if isinstance(exc, ConfigurationError):
                self.application["issues"] = [exc.public()]
            self.persist_application()

    def preview(self):
        import tomllib

        with self.files.locked():
            snapshot = self.files.snapshot()
            self.files.validate_all(snapshot.contents, snapshot.resources)
        before = self.loaded_snapshot.contents if self.loaded_snapshot else {}
        fields = set()

        def compare(old, new, prefix=()):
            for key in old.keys() | new.keys():
                a, b = old.get(key), new.get(key)
                if a == b:
                    continue
                if isinstance(a, dict) and isinstance(b, dict):
                    compare(a, b, (*prefix, key))
                else:
                    fields.add(".".join((*prefix, key)))

        for name in before.keys() | snapshot.contents.keys():
            compare(
                tomllib.loads(before.get(name, b"").decode()),
                tomllib.loads(snapshot.contents.get(name, b"").decode()),
            )
        for ident in snapshot.resources.keys() | (
            self.loaded_snapshot.resources.keys() if self.loaded_snapshot else set()
        ):
            if snapshot.resources.get(ident) != (
                self.loaded_snapshot.resources.get(ident) if self.loaded_snapshot else None
            ):
                fields.add("resource:" + ident)
        return {
            "version": snapshot.version,
            "fields": sorted(fields),
            "running": self.process is not None and self.process.returncode is None,
            "required_steps": self.application_steps(snapshot),
        }

    def apply(self, version, mode, confirmed):
        from assistant_runtime.core.configuration.files import Conflict

        if confirmed is not True or not isinstance(mode, str) or mode not in {"wait", "force"}:
            raise ValueError("Explicit restart confirmation and mode required")
        if self.operation.locked():
            raise ValueError("Business lifecycle operation is already active")
        if self.apply_task and not self.apply_task.done():
            raise ValueError("Configuration application is already active")
        with self.files.locked():
            snapshot = self.files.snapshot()
            if snapshot.version != version:
                raise Conflict("Reload the configuration before applying")
            self.files.validate_all(snapshot.contents, snapshot.resources)
        self.application = {
            "phase": "preparing",
            "version": version,
            "mode": mode,
            "required_steps": self.application_steps(snapshot),
        }
        self.persist_application()
        self.apply_task = asyncio.create_task(self._apply(snapshot, mode), name="configuration-apply")
        return dict(self.application)

    async def _apply(self, snapshot, mode):
        try:
            if self.process and self.process.returncode is None:
                self.application_phase("waiting")
                await self.request({"command": "management-pause"})
                if mode == "wait":
                    while True:
                        work = await self.request({"command": "management-work"})
                        self.application["blockers"] = work["blockers"]
                        if work["ready"]:
                            break
                        await asyncio.sleep(0.25)
                else:
                    self.application_phase("cancelling")
                    try:
                        await self.request({"command": "management-cancel"})
                    except (OSError, ValueError, TimeoutError):
                        pass  # Shutdown retains unknown outcomes; it never replays operations.
            self.application_phase("restarting")
            await self.stop()
            await self.start(snapshot)
            if self.outcome in {"running", "degraded"} and getattr(self, "management_applied", None):
                try:
                    self.application["management_address"] = await self.management_applied()
                    self.application["required_steps"] = [
                        s
                        for s in self.application.get("required_steps", [])
                        if s["component"] != "management"
                    ]
                except (ValueError, OSError):
                    self.application.setdefault("required_steps", []).append(
                        {
                            "component": "management",
                            "message": "管理地址或监听配置未能切换；旧入口继续可用，请检查端口占用后重启整个服务。",
                        }
                    )
            self.application.update(
                phase=(
                    "partially_applied"
                    if self.application.get("required_steps") or self.outcome == "degraded"
                    else "applied"
                )
                if self.outcome in {"running", "degraded"}
                else "failed",
                blockers=[],
            )
            self.persist_application()
        except asyncio.CancelledError:
            raise
        except Exception:
            self.application_phase("failed")
            if self.process and self.process.returncode is None:
                try:
                    await self.request({"command": "management-resume"})
                except (OSError, ValueError, TimeoutError):
                    self.application["resume_failed"] = True
            self.persist_application()

    async def abort_apply(self):
        if (self.application.get("hot") and self.application["phase"] != "waiting") or self.application[
            "phase"
        ] not in {"preparing", "waiting"}:
            raise ValueError("Only waiting for a safe configuration boundary can be withdrawn")
        self.apply_task.cancel()
        await asyncio.gather(self.apply_task, return_exceptions=True)
        if self.process and self.process.returncode is None:
            await self.request({"command": "management-resume"})
        self.application = {"phase": "withdrawn"}
        self.persist_application()
        return dict(self.application)

    async def stop(self):
        process = self.process
        if process and process.returncode is None:
            try:
                await self.request({"command": "shutdown"})
            except (OSError, ValueError, TimeoutError):
                pass
            await system.stop_process(process, timeout=self.settings.shutdown_seconds + 5, graceful=True)
        if process is not None and process.returncode == 0:
            (self.state / "business-owner.json").unlink(missing_ok=True)
            system.sync_directory(self.state)
        if self.logfile:
            self.logfile.close()
            self.logfile = None
        self.outcome = "stopped"


async def serve(settings, *, stop=None):
    from assistant_runtime.core.configuration.console import Console

    state = settings.root / "state/configuration"
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (state / "manager.lock").open("a+b") as lock:
        system.private(lock.name)
        system.lock(lock, blocking=False)
        # Refuse to supervise an instance already owned by a separately started worker.
        with (settings.root / "state/service.lock").open("a+b") as business_lock:
            system.lock(business_lock, blocking=False)
        business = BusinessProcess(settings)
        console = Console(business)
        if stop is None:
            stop = asyncio.Event()
            system.stop_signals(stop.set)
        startup = None
        try:
            await console.start()
            startup = asyncio.create_task(business.start())
            await stop.wait()
        finally:
            if startup and not startup.done():
                startup.cancel()
                await asyncio.gather(startup, return_exceptions=True)
            if business.apply_task and not business.apply_task.done():
                business.apply_task.cancel()
                await asyncio.gather(business.apply_task, return_exceptions=True)
            await business.stop()
            await console.close()
