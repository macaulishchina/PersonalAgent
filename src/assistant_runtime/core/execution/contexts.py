"""Durable context/workspace references, independent of one execution outcome."""

import json
import math
import time

from .authority import uid, identifier


class ContextRecords:
    def __init__(self, service):
        self.service, self.db = service, service.records.db
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS execution_resources(
                id TEXT PRIMARY KEY, kind TEXT NOT NULL, controller TEXT NOT NULL,
                owner TEXT NOT NULL, revision INTEGER NOT NULL, data TEXT NOT NULL
            );
        """)

    @staticmethod
    def expiry(value):
        if type(value) not in (int, float) or not math.isfinite(value) or value <= time.time():
            raise ValueError("Resource requires a finite future expires_at")
        return value

    def get(self, kind, ident):
        raw = self.db.execute(
            "SELECT * FROM execution_resources WHERE id=? AND kind=?", (uid(ident), kind)
        ).fetchone()
        if raw is None:
            raise PermissionError("Execution resource is unavailable")
        row = dict(zip(("id", "kind", "controller", "owner", "revision", "data"), raw))
        row["data"] = json.loads(row["data"])
        return row

    def owned(self, principal, kind, ident):
        row = self.get(kind, ident)
        if row["controller"] != principal:
            raise PermissionError("Execution resource is unavailable")
        self.service.authority.controller(principal, row["owner"])
        return row

    def save(self, row, **changes):
        data = {**row["data"], **changes}
        with self.db:
            cursor = self.db.execute(
                "UPDATE execution_resources SET revision=revision+1,data=? WHERE id=? AND revision=?",
                (json.dumps(data), row["id"], row["revision"]),
            )
            if cursor.rowcount != 1:
                raise ValueError("Execution resource revision conflict")
        return self.get(row["kind"], row["id"])

    def create(self, principal, kind, ident, owner, data):
        self.service.authority.controller(principal, owner)
        with self.db:
            self.db.execute(
                "INSERT INTO execution_resources VALUES(?,?,?,?,1,?)",
                (ident, kind, principal, owner, json.dumps(data)),
            )
        return self.get(kind, ident)

    def view(self, row):
        data = row["data"]
        result = {key: row[key] for key in ("id", "kind", "owner", "revision")}
        result.update({key: value for key, value in data.items() if key not in {"native_session", "intent"}})
        result["expired"] = data["expires_at"] <= time.time()
        if row["kind"] == "context":
            result["compression"] = (
                dict(
                    self.db.execute(
                        "SELECT o.state,count(*) FROM execution_operations o JOIN agent_runs r ON r.id=o.execution_id WHERE json_extract(r.data,'$.context_id')=? AND o.kind='model' AND json_extract(o.data,'$.compaction')=1 GROUP BY o.state",
                        (row["id"],),
                    )
                )
                if self.db.execute("SELECT 1 FROM sqlite_master WHERE name='execution_operations'").fetchone()
                else {}
            )
            result["recoverable"] = (
                bool(data.get("native_session")) and data["state"] == "ready" and not result["expired"]
            )
        return result

    def workspace_create(self, principal, args):
        required = {"workspace_id", "owner", "version", "usage_scope_id", "expires_at"}
        if not required <= set(args) or set(args) - required - {"usage_limits"}:
            raise ValueError("Workspace requires workspace_id, owner, version, usage_scope_id and expires_at")
        from assistant_runtime.core.ai_usage.controlled import validate

        validate(args.get("usage_limits", {}))
        ident = uid(args["workspace_id"])
        for key in ("owner", "version", "usage_scope_id"):
            identifier(args[key])
        intent = json.dumps({**args, "workspace_id": ident}, sort_keys=True)
        if self.db.execute("SELECT 1 FROM execution_resources WHERE id=?", (ident,)).fetchone():
            row = self.owned(principal, "workspace", ident)
            if row["data"]["intent"] != intent:
                raise ValueError("Workspace ID reused with different content")
            return self.view(row)
        self.expiry(args["expires_at"])
        row = self.create(
            principal,
            "workspace",
            ident,
            args["owner"],
            {
                "intent": intent,
                "version": args["version"],
                "usage_scope_id": args["usage_scope_id"],
                "expires_at": args["expires_at"],
                "active_execution": None,
                "released": False,
                "usage_limits": args.get("usage_limits", {}),
            },
        )
        return self.view(row)

    def context_create(self, principal, args):
        if set(args) != {"context_id", "grant_id", "expires_at", "model", "effort"}:
            raise ValueError("Context requires context_id, grant_id, expires_at, model and effort")
        ident = uid(args["context_id"])
        grant = self.service.authority.owned(principal, args["grant_id"])
        binding = grant["binding"]
        if binding["context_id"] != ident:
            raise PermissionError("Grant belongs to another context")
        self.service.authority.verify(principal, grant["id"], binding, "execute", grant["generation"])
        intent = json.dumps({**args, "context_id": ident, "grant_id": grant["id"]}, sort_keys=True)
        if self.db.execute("SELECT 1 FROM execution_resources WHERE id=?", (ident,)).fetchone():
            row = self.owned(principal, "context", ident)
            if row["data"]["intent"] != intent:
                raise ValueError("Context ID reused with different content")
            return self.view(row)
        workspace = self.owned(principal, "workspace", binding["workspace_id"])
        wd = workspace["data"]
        if (
            workspace["owner"] != binding["owner"]
            or wd["version"] != binding["workspace_version"]
            or wd["usage_scope_id"] != binding["usage_scope_id"]
        ):
            raise PermissionError("Grant does not match workspace owner, version or usage scope")
        if wd["released"] or wd["expires_at"] < self.expiry(args["expires_at"]):
            raise ValueError("Workspace must be retained through the requested context expiry")
        for key in ("model", "effort"):
            identifier(args[key])
        row = self.create(
            principal,
            "context",
            ident,
            binding["owner"],
            {
                "intent": intent,
                "binding": binding,
                "grant_id": grant["id"],
                "grant_generation": grant["generation"],
                "model": args["model"],
                "effort": args["effort"],
                "expires_at": args["expires_at"],
                "state": "ready",
                "native_session": None,
                "last_execution": None,
            },
        )
        return self.view(row)

    def verify(self, row, action="execute"):
        data = row["data"]
        if data["state"] in {"uncertain", "closed"} or data["expires_at"] <= time.time():
            raise PermissionError("Context is uncertain, closed or expired")
        self.service.authority.verify(
            row["controller"], data["grant_id"], data["binding"], action, data["grant_generation"]
        )
        workspace = self.owned(row["controller"], "workspace", data["binding"]["workspace_id"])
        if workspace["data"]["released"] or workspace["data"]["expires_at"] <= time.time():
            raise PermissionError("Workspace is released or expired")
        if workspace["data"]["version"] != data["binding"]["workspace_version"]:
            raise PermissionError("Workspace version changed")
        return workspace

    def reserve(self, row, execution, revision):
        if type(revision) is not int or revision != row["revision"]:
            raise ValueError("Context revision conflict")
        if row["data"]["state"] != "ready":
            raise RuntimeError("Context already has an active or uncertain execution")
        workspace = self.verify(row)
        if workspace["data"]["active_execution"] is not None:
            raise RuntimeError("Workspace has an active or unconfirmed conflicting execution")
        # Both references are updated in one transaction, without await boundaries.
        with self.db:
            for current, changes in (
                (row, {"state": "active", "last_execution": execution}),
                (workspace, {"active_execution": execution}),
            ):
                cursor = self.db.execute(
                    "UPDATE execution_resources SET revision=revision+1,data=? WHERE id=? AND revision=?",
                    (json.dumps({**current["data"], **changes}), current["id"], current["revision"]),
                )
                if cursor.rowcount != 1:
                    raise ValueError("Execution resource revision conflict")
        return self.get("context", row["id"])

    def retain(self, principal, kind, args):
        if set(args) != {"id", "expires_at", "revision"}:
            raise ValueError("Retain requires id, expires_at and revision")
        row = self.owned(principal, kind, args["id"])
        if type(args["revision"]) is not int or row["revision"] != args["revision"]:
            raise ValueError("Resource revision conflict")
        expiry = self.expiry(args["expires_at"])
        if (
            row["data"]["expires_at"] <= time.time()
            or row["data"].get("state") == "closed"
            or row["data"].get("released")
        ):
            raise ValueError("Expired or released resources cannot be revived")
        if kind == "context":
            workspace = self.verify(row)
            if workspace["data"]["expires_at"] < expiry:
                raise ValueError("Retain the workspace before extending its context")
        return self.view(self.save(row, expires_at=max(expiry, row["data"]["expires_at"])))

    def authorize(self, principal, args):
        if set(args) != {"context_id", "grant_id", "revision"}:
            raise ValueError("Context authorization requires context_id, grant_id and revision")
        row = self.owned(principal, "context", args["context_id"])
        if type(args["revision"]) is not int or row["revision"] != args["revision"]:
            raise ValueError("Context revision conflict")
        if row["data"]["state"] != "ready" or row["data"]["expires_at"] <= time.time():
            raise PermissionError("Context must be idle and valid to change its grant")
        grant = self.service.authority.owned(principal, args["grant_id"])
        previous, incoming = row["data"]["binding"], grant["binding"]
        if any(previous[key] != incoming[key] for key in previous if key != "actor"):
            raise PermissionError("New authorization cannot change context resource or usage bindings")
        self.service.authority.verify(principal, grant["id"], incoming, "execute", grant["generation"])
        return self.view(
            self.save(row, binding=incoming, grant_id=grant["id"], grant_generation=grant["generation"])
        )

    def close(self, principal, kind, args):
        if set(args) != {"id", "revision"}:
            raise ValueError("Release requires id and revision")
        row = self.owned(principal, kind, args["id"])
        if type(args["revision"]) is not int or row["revision"] != args["revision"]:
            raise ValueError("Resource revision conflict")
        if kind == "context":
            workspace = self.get("workspace", row["data"]["binding"]["workspace_id"])
            if row["data"]["state"] == "active" or (
                row["data"].get("last_execution")
                and workspace["data"]["active_execution"] == row["data"]["last_execution"]
            ):
                raise RuntimeError("Active or unconfirmed context resources cannot be released")
            closed = self.save(row, state="closed")
            for ident, raw, state in self.db.execute("SELECT id,data,state FROM agent_runs"):
                if json.loads(raw).get("context_id") == row["id"] and state != "unknown":
                    self.service.files.finish("executions/" + ident)
            return self.view(closed)
        if row["data"]["active_execution"]:
            raise RuntimeError("Active or unconfirmed workspace cannot be released")
        for (raw,) in self.db.execute("SELECT data FROM execution_resources WHERE kind='context'"):
            context = json.loads(raw)
            if context["binding"]["workspace_id"] == row["id"] and context["state"] != "closed":
                raise RuntimeError("Workspace is still referenced by a retained context")
        return self.view(self.save(row, released=True))

    def finish(self, ident, execution, *, stopped):
        row = self.get("context", ident)
        if row["data"].get("last_execution") != execution:
            raise ValueError("Execution does not own this context")
        outcome = self.service.records.get(execution)["state"]
        settled = stopped and outcome in {"completed", "failed", "cancelled", "interrupted"}
        workspace = self.get("workspace", row["data"]["binding"]["workspace_id"])
        with self.db:
            self.db.execute(
                "UPDATE execution_resources SET revision=revision+1,data=? WHERE id=?",
                (json.dumps({**row["data"], "state": "ready" if settled else "uncertain"}), ident),
            )
            if stopped and workspace["data"]["active_execution"] == execution:
                self.db.execute(
                    "UPDATE execution_resources SET revision=revision+1,data=? WHERE id=?",
                    (json.dumps({**workspace["data"], "active_execution": None}), workspace["id"]),
                )
        return self.get("context", ident)

    def recover(self):
        for ident, raw in self.db.execute(
            "SELECT id,data FROM execution_resources WHERE kind='context'"
        ).fetchall():
            data = json.loads(raw)
            if data["state"] == "active":
                # No stopped proof is inferred from a host restart. Keep workspace ownership.
                self.save(self.get("context", ident), state="uncertain")
