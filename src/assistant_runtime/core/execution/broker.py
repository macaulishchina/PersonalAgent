"""Model-facing resource operations bound to one admitted execution and grant."""

import asyncio
import hashlib
import json
import re
import threading
import time
import uuid
import weakref
from functools import partial

from assistant_runtime.api.lifecycle import wait_thread
from .controlled_files import ControlledFiles
from .authority import uid


class ResourceBroker:
    def __init__(self, service):
        self.service, self.db = service, service.records.db
        self.files = ControlledFiles(service)
        self.locks = weakref.WeakValueDictionary()
        self.cancelled, self.active, self.runners = {}, {}, {}
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS controlled_operations(
                id TEXT PRIMARY KEY, execution_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
                state TEXT NOT NULL, result TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS controlled_fences(
                execution_id TEXT PRIMARY KEY, host_start_id TEXT NOT NULL,
                state TEXT NOT NULL, evidence TEXT NOT NULL
            );
            UPDATE controlled_operations SET state='unknown' WHERE state='running';
        """)

    def lock(self, workspace):
        return self.locks.setdefault(workspace, asyncio.Lock())

    def open(self, execution):
        with self.db:
            self.db.execute(
                "INSERT INTO controlled_fences VALUES(?,?,'open','{}')",
                (execution, self.service.records.operations.host_start_id),
            )
        self.cancelled[execution] = threading.Event()

    def admitted(self, execution, action):
        run = self.service.records.get(execution)
        context = self.service.contexts.get("context", run["data"]["context_id"])
        if (
            run["state"] not in {"dispatching", "running", "waiting_user"}
            or context["data"]["state"] != "active"
            or context["data"]["last_execution"] != execution
        ):
            raise PermissionError("Execution no longer owns an active resource context")
        fence = self.db.execute(
            "SELECT host_start_id,state FROM controlled_fences WHERE execution_id=?", (execution,)
        ).fetchone()
        if (
            fence != (self.service.records.operations.host_start_id, "open")
            or execution not in self.cancelled
            or self.cancelled[execution].is_set()
        ):
            raise PermissionError("Execution resource access is fenced")
        workspace = self.service.contexts.verify(context, action)
        if workspace["data"]["active_execution"] != execution:
            raise PermissionError("Execution does not own this workspace")
        grant = self.service.authority.get(context["data"]["grant_id"])

        def guard():
            return (
                not self.cancelled[execution].is_set()
                and self.service.authority.live(grant)
                and time.time() < min(context["data"]["expires_at"], workspace["data"]["expires_at"])
            )

        return context, workspace, guard

    async def collect(self, principal, args):
        if set(args) != {"id", "revision"}:
            raise ValueError("Workspace collection requires id and revision")
        async with self.lock(uid(args["id"])):
            row = self.service.contexts.owned(principal, "workspace", args["id"])
            if type(args["revision"]) is not int or args["revision"] != row["revision"]:
                raise ValueError("Workspace revision conflict")
            if not row["data"]["released"] or row["data"]["active_execution"]:
                raise PermissionError(
                    "Close every context and release the verified idle workspace before collection"
                )
            await wait_thread(self.files.purge, row["id"])
            return self.service.contexts.view(self.service.contexts.save(row, collected=True))

    async def prepare(self, principal, args):
        ident = uid(args.get("workspace_id"))
        async with self.lock(ident):
            # State validation and write admission are serialized with new rounds.
            # Source preparation is a trusted control operation, never a model tool.
            return self.files.prepare(principal, args)

    async def register_check(self, principal, args):
        if set(args) != {"workspace_id", "name", "script", "timeout_seconds"}:
            raise ValueError("Check requires workspace_id, name, script and timeout_seconds")
        ident = uid(args["workspace_id"])
        if not isinstance(args["name"], str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", args["name"]):
            raise ValueError("Invalid fixed check name")
        if (
            not isinstance(args["script"], str)
            or not args["script"].isascii()
            or not 0 < len(args["script"].encode()) <= 65536
        ):
            raise ValueError("Fixed cmd check must be nonempty ASCII text of at most 64 KiB")
        if type(args["timeout_seconds"]) is not int or not 1 <= args["timeout_seconds"] <= 60:
            raise ValueError("Check timeout must be 1..60 seconds")
        async with self.lock(ident):
            workspace = self.service.contexts.owned(principal, "workspace", ident)
            if (
                workspace["data"]["released"]
                or self.service.contexts.view(workspace)["expired"]
                or workspace["data"]["active_execution"]
            ):
                raise RuntimeError("Workspace is unavailable or occupied")
            for (raw,) in self.db.execute("SELECT data FROM execution_resources WHERE kind='context'"):
                if json.loads(raw)["binding"]["workspace_id"] == ident:
                    raise ValueError("Check declarations are frozen once a context exists")
            root = self.files.ensure(ident)
            name = args["name"] + ".cmd"
            checks = dict(workspace["data"].get("checks", {}))
            saved = self.files.write(
                root / "checks", name, args["script"], checks.get(args["name"], {}).get("sha256")
            )
            checks[args["name"]] = {**saved, "timeout_seconds": args["timeout_seconds"]}
            self.service.contexts.save(workspace, checks=checks)
            return checks[args["name"]]

    async def request(self, execution, operation, action, args):
        if not isinstance(operation, str) or not 1 <= len(operation) <= 256:
            raise ValueError("Resource operation requires an engine-bound operation ID")
        allowed = {
            "read": {"area", "path", "offset", "length"},
            "write": {"area", "path", "text", "expected_sha256"},
            "check": {"name"},
        }
        required = {"read": {"area", "path"}, "write": allowed["write"], "check": allowed["check"]}
        if (
            action not in allowed
            or not isinstance(args, dict)
            or set(args) - allowed[action]
            or not required[action] <= set(args)
        ):
            raise ValueError("Unknown controlled operation or fields")
        context, workspace, guard = self.admitted(execution, "edit" if action == "write" else action)
        ident = str(uuid.uuid5(uuid.NAMESPACE_URL, json.dumps([execution, operation])))
        fingerprint = hashlib.sha256(json.dumps([action, args], sort_keys=True).encode()).hexdigest()
        async with self.lock(workspace["id"]):
            context, workspace, guard = self.admitted(execution, "edit" if action == "write" else action)
            previous = self.db.execute(
                "SELECT fingerprint,state,result FROM controlled_operations WHERE id=?", (ident,)
            ).fetchone()
            if previous:
                if previous[0] != fingerprint:
                    raise ValueError("Operation ID reused with different content")
                return {"operation_id": ident, "state": previous[1], "result": json.loads(previous[2])}
            if action == "write" and args["area"] not in {"work", "artifacts"}:
                raise PermissionError("Read-only source material cannot be edited")
            # Persist before any file opening or process creation. Unknown intents
            # are never executed again after an interrupted caller or host restart.
            with self.db:
                self.db.execute(
                    "INSERT INTO controlled_operations VALUES(?,?,?,'running','{}')",
                    (ident, execution, fingerprint),
                )
            parent = self.db.execute(
                "SELECT id FROM execution_operations WHERE execution_id=? AND kind='tool' AND native_id=?",
                (execution, operation),
            ).fetchone()
            with self.db:
                self.service.records.operations.observe(
                    execution,
                    ident,
                    {
                        "kind": "controlled_operation",
                        "state": "running",
                        "action": action,
                        "parent_operation_id": parent[0] if parent else None,
                        "grant_id": context["data"]["grant_id"],
                        "grant_generation": context["data"]["grant_generation"],
                        "actor": context["data"]["binding"]["actor"],
                        "workspace_version": context["data"]["binding"]["workspace_version"],
                    },
                )
            active = self.active.setdefault(execution, set())
            done = asyncio.Event()
            active.add(done)
            state, result = "unknown", {}
            runner = None
            try:
                if action == "check":
                    check = workspace["data"].get("checks", {}).get(args["name"])
                    if check is None:
                        raise ValueError("Unknown configured fixed check")
                    from assistant_runtime.platforms.windows_isolation import IsolatedCheck

                    area = self.files.execution_area(workspace["id"], execution)
                    # Each command has a distinct log/temp directory.
                    temporary = area / "temporary" / ident
                    temporary.mkdir()
                    runner = IsolatedCheck(
                        self.files.root(workspace["id"]),
                        self.files.root(workspace["id"]) / "checks" / (args["name"] + ".cmd"),
                        temporary,
                        timeout=check["timeout_seconds"],
                        authorized=guard,
                        script_sha256=check["sha256"],
                        round_directory=area,
                    )
                    self.runners[ident] = runner
                    result = await runner.execute()
                    state = "completed" if result["exit_code"] == 0 and not result.get("reason") else "failed"
                else:
                    area = self.files.area(workspace["id"], args["area"], execution)
                    if action == "read":
                        invoke = partial(
                            self.files.read,
                            area,
                            args["path"],
                            authorized=guard,
                            offset=args.get("offset", 0),
                            length=args.get("length", 128 * 1024),
                        )
                    else:
                        invoke = partial(
                            self.files.write,
                            area,
                            args["path"],
                            args["text"],
                            args["expected_sha256"],
                            authorized=guard,
                        )
                    result = await wait_thread(invoke, cancel=self.cancelled[execution].set)
                    state = "completed"
            except (ValueError, PermissionError) as error:
                state = "failed"
                result = {"error": str(error)}
            except BaseException as error:
                result = {"error": type(error).__name__}
                self.cancelled[execution].set()
                self.service.records.update(
                    execution, "unknown", error={"code": "controlled_operation_unconfirmed"}
                )
                signal = self.service.finished.get(execution)
                if signal:
                    signal.set()
                raise
            finally:
                if runner is not None:
                    result = {
                        **result,
                        "isolation_evidence": {k: v for k, v in runner.evidence.items() if k != "output"},
                    }
                    if "output" in result:
                        output = result["output"]
                        result["output"] = output[:16384]
                        result["output_truncated"] = len(output) > 16384
                    if not runner.stopped:
                        state = "unknown"
                        self.cancelled[execution].set()
                        self.service.records.update(
                            execution, "unknown", error={"code": "controlled_stop_unconfirmed"}
                        )
                        signal = self.service.finished.get(execution)
                        if signal:
                            signal.set()
                with self.db:
                    self.db.execute(
                        "UPDATE controlled_operations SET state=?,result=? WHERE id=?",
                        (state, json.dumps(result), ident),
                    )
                    self.service.records.operations.observe(
                        execution,
                        ident,
                        {
                            "kind": "controlled_operation",
                            "state": state,
                            "action": action,
                            "tree_stopped": runner.stopped if runner is not None else None,
                        },
                    )
                active.discard(done)
                done.set()
            return {"operation_id": ident, "state": state, "result": result}

    def cancel(self, execution):
        event = self.cancelled.get(execution)
        if event:
            event.set()
        for ident, runner in self.runners.items():
            row = self.db.execute(
                "SELECT execution_id FROM controlled_operations WHERE id=?", (ident,)
            ).fetchone()
            if row and row[0] == execution:
                runner.cancel()

    async def fence(self, execution):
        self.cancel(execution)
        active = list(self.active.get(execution, ()))
        for event in active:
            await event.wait()
        stopped = True
        for ident, runner in list(self.runners.items()):
            row = self.db.execute(
                "SELECT execution_id FROM controlled_operations WHERE id=?", (ident,)
            ).fetchone()
            if row and row[0] == execution:
                stopped = stopped and runner.stopped
                if runner.stopped:
                    self.runners.pop(ident, None)
        current = self.db.execute(
            "SELECT host_start_id,state FROM controlled_fences WHERE execution_id=?", (execution,)
        ).fetchone()
        if current is not None and current[1] == "closed":
            return True
        if current is None or current[0] != self.service.records.operations.host_start_id:
            return False  # No current host handle proves a prior host's stop.
        evidence = {
            "local_writes": "fenced" if stopped else "unknown",
            "controlled_processes": "stopped" if stopped else "unknown",
            "external_effects": "not_exposed",
            "host_start_id": current[0],
        }
        with self.db:
            self.db.execute(
                "UPDATE controlled_fences SET state=?,evidence=? WHERE execution_id=?",
                ("closed" if stopped else "uncertain", json.dumps(evidence), execution),
            )
        return stopped
