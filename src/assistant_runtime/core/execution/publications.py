"""Controlled publications reuse immutable workfiles storage and read leases."""

import json
import time
from assistant_runtime.api.files import FileRequest


class Publications:
    def __init__(self, service):
        self.service = service
        self.db = service.records.db
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS artifact_archives(id TEXT PRIMARY KEY,controller TEXT NOT NULL,receipt TEXT NOT NULL,acknowledged REAL NOT NULL)"
        )
        self.db.commit()

    async def request(self, principal, action, args):
        args = dict(args)
        grant_id = args.pop("grant_id", None)

        def authorize():
            grant = self.service.authority.owned(principal, grant_id)
            if action == "artifact_finalize":
                run = self.service.records.get(args.get("execution_id"))
                binding = run["data"].get("control_binding")
                if not binding:
                    raise PermissionError("Controlled execution is required")
                context = self.service.contexts.owned(principal, "context", binding["context_id"])
                fence = self.db.execute(
                    "SELECT state FROM controlled_fences WHERE execution_id=?", (run["id"],)
                ).fetchone()
                if fence != ("closed",):
                    raise PermissionError("Execution writes must be verified stopped before publication")
                metadata = dict(
                    source=self.service.broker.files.area(binding["workspace_id"], "artifacts", run["id"]),
                    control_binding=binding,
                    controller=context["controller"],
                )
            else:
                row = self.service.files.publications.get(args.get("id"), controlled=True)
                binding = row["data"].get("control_binding")
                if not binding or row["data"].get("controller") != principal:
                    raise PermissionError("Publication is unavailable to this controller")
                metadata = {}
            # A new member can be authorized to archive an old result; all resource
            # dimensions remain fixed. Actor comes from the new trusted grant.
            expected = {**binding, "actor": grant["binding"]["actor"]}
            self.service.authority.verify(
                principal,
                grant["id"],
                expected,
                "read" if action in {"artifact_get", "artifact_read"} else "publish",
                grant["generation"],
            )
            return metadata

        if action == "artifact_acknowledge":
            if set(args) != {"id", "sha256", "bytes", "archive_reference"}:
                raise ValueError("Archive acknowledgement requires id, sha256, bytes and archive_reference")
            async with self.service.files.publications.lock:
                authorize()
                row = self.service.files.publications.get(args["id"], controlled=True)
                if (
                    row["state"] != "published"
                    or row["data"].get("sha256") != args["sha256"]
                    or row["data"].get("bytes") != args["bytes"]
                ):
                    raise ValueError("Archive acknowledgement does not match published bytes")
                reference = args["archive_reference"]
                if not isinstance(reference, str) or not 1 <= len(reference) <= 1024:
                    raise ValueError("A durable external archive reference is required")
                receipt = json.dumps(args, sort_keys=True)
                prior = self.db.execute(
                    "SELECT receipt FROM artifact_archives WHERE id=?", (row["id"],)
                ).fetchone()
                if prior and prior[0] != receipt:
                    raise ValueError("Publication already acknowledged with a different archive receipt")
                with self.db:
                    self.db.execute(
                        "INSERT OR IGNORE INTO artifact_archives VALUES(?,?,?,?)",
                        (row["id"], principal, receipt, time.time()),
                    )
                return {
                    "id": row["id"],
                    "archived": True,
                    "archive_reference": reference,
                    "evidence": "controller_acknowledgement",
                }
        result = await self.service.files.publications.request(
            FileRequest("finalize" if action == "artifact_finalize" else action, args), _control=authorize
        )
        acknowledged = self.db.execute(
            "SELECT receipt FROM artifact_archives WHERE id=?", (result["id"],)
        ).fetchone()
        return {**result, "archived": bool(acknowledged)}
