"""Host-bound, short-lived resource grants; no model-supplied issuer identity."""

import json
import math
import time
import uuid

BINDING = {"owner", "actor", "context_id", "workspace_id", "workspace_version", "usage_scope_id"}
ACTIONS = {"execute", "read", "edit", "check", "publish"}


def identifier(value):
    if (
        not isinstance(value, str)
        or not value
        or len(value.encode()) > 200
        or any(ord(c) < 32 for c in value)
    ):
        raise ValueError("Invalid control identity")
    return value


def uid(value):
    try:
        return str(uuid.UUID(value))
    except (ValueError, TypeError, AttributeError):
        raise ValueError("Control request requires a stable UUID") from None


class Authority:
    """Only composition-root-bound controllers call issue/renew/revoke.

    An issuer is trusted installed code authenticated by its injected handle, not
    a value read from request JSON. Controllers must recheck upstream business
    permission before each renewal; PA checks their configured delegation ceiling.
    """

    def __init__(self, db, controllers, *, wall=time.time, monotonic=time.monotonic):
        self.db, self.controllers = db, controllers
        self.wall, self.monotonic = wall, monotonic
        self.anchors = {}
        self.deadlines = {}  # Deliberately not restored after a host restart.
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS execution_grants(
                id TEXT PRIMARY KEY, controller TEXT NOT NULL, binding TEXT NOT NULL,
                actions TEXT NOT NULL, expires REAL NOT NULL, generation INTEGER NOT NULL,
                revoked INTEGER NOT NULL, intent TEXT NOT NULL
            );
        """)

    def controller(self, principal, owner):
        # No wildcard owner or default trusted controller.
        if owner not in self.controllers.get(principal, ()):
            raise PermissionError("Control service is not authorized for this resource owner")

    def duration(self, expires):
        if type(expires) not in (int, float) or not math.isfinite(expires):
            raise ValueError("Grant expiry must be finite UTC Unix seconds")
        duration = expires - self.wall()
        if not 0 < duration <= 60:
            raise ValueError("Grant lease must be in the future and at most 60 seconds")
        return duration

    def get(self, ident):
        raw = self.db.execute("SELECT * FROM execution_grants WHERE id=?", (uid(ident),)).fetchone()
        if raw is None:
            raise PermissionError("Resource grant is unavailable")
        row = dict(
            zip(
                ("id", "controller", "binding", "actions", "expires_at", "generation", "revoked", "intent"),
                raw,
            )
        )
        row["binding"], row["actions"] = json.loads(row["binding"]), json.loads(row["actions"])
        return row

    def owned(self, principal, ident):
        row = self.get(ident)
        if row["controller"] != principal:
            raise PermissionError("Resource grant is unavailable")
        self.controller(principal, row["binding"]["owner"])
        return row

    def view(self, row):
        anchor = self.anchors.get(row["id"])
        clock_changed = (
            anchor is not None and abs((self.wall() - anchor[0]) - (self.monotonic() - anchor[1])) > 5
        )
        return {key: value for key, value in row.items() if key != "intent"} | {
            "verified_this_start": row["id"] in self.deadlines,
            "expired": clock_changed
            or row["expires_at"] <= self.wall()
            or self.deadlines.get(row["id"], 0) <= self.monotonic(),
            "clock_changed": clock_changed,
        }

    def issue(self, principal, args):
        if set(args) != {"grant_id", "binding", "actions", "expires_at"}:
            raise ValueError("Grant requires grant_id, binding, actions and expires_at")
        ident = uid(args["grant_id"])
        binding = args["binding"]
        if not isinstance(binding, dict) or set(binding) != BINDING:
            raise ValueError("Grant binding is incomplete")
        for value in binding.values():
            identifier(value)
        self.controller(principal, binding["owner"])
        actions = args["actions"]
        if (
            not isinstance(actions, list)
            or not actions
            or any(type(v) is not str for v in actions)
            or set(actions) - ACTIONS
            or len(actions) != len(set(actions))
        ):
            raise ValueError("Invalid resource actions")
        intent = json.dumps({**args, "grant_id": ident, "actions": sorted(actions)}, sort_keys=True)
        if self.db.execute("SELECT 1 FROM execution_grants WHERE id=?", (ident,)).fetchone():
            row = self.owned(principal, ident)
            if row["intent"] != intent:
                raise ValueError("Grant ID reused with different content")
            return self.view(row)  # Repeating issue never renews a lease.
        duration = self.duration(args["expires_at"])
        with self.db:
            self.db.execute(
                "INSERT INTO execution_grants VALUES(?,?,?,?,?,1,0,?)",
                (
                    ident,
                    principal,
                    json.dumps(binding, sort_keys=True),
                    json.dumps(sorted(actions)),
                    args["expires_at"],
                    intent,
                ),
            )
        self.anchors[ident] = (self.wall(), self.monotonic())
        self.deadlines[ident] = self.monotonic() + duration
        return self.view(self.get(ident))

    def renew(self, principal, args):
        if set(args) != {"grant_id", "generation", "expires_at"}:
            raise ValueError("Renew requires grant_id, generation and expires_at")
        row = self.owned(principal, args["grant_id"])
        if row["revoked"]:
            raise PermissionError("Revoked grants cannot be renewed")
        if type(args["generation"]) is not int or args["generation"] != row["generation"]:
            raise ValueError("Grant generation conflict")
        duration = self.duration(args["expires_at"])
        with self.db:
            self.db.execute(
                "UPDATE execution_grants SET expires=? WHERE id=?", (args["expires_at"], row["id"])
            )
        self.anchors[row["id"]] = (self.wall(), self.monotonic())
        self.deadlines[row["id"]] = self.monotonic() + duration
        return self.view(self.get(row["id"]))

    def revoke(self, principal, args):
        if set(args) != {"grant_id"}:
            raise ValueError("Revoke requires grant_id")
        row = self.owned(principal, args["grant_id"])
        with self.db:
            self.db.execute(
                "UPDATE execution_grants SET revoked=1,generation=generation+1 WHERE id=? AND revoked=0",
                (row["id"],),
            )
        self.deadlines.pop(row["id"], None)
        self.anchors.pop(row["id"], None)
        return self.view(self.get(row["id"]))

    def live(self, grant):
        """Read-only lease check for an already admitted off-thread operation.

        This never creates authority. Its snapshot was verified on the control
        loop before admission; revocation removes the live deadline synchronously.
        """
        anchor = self.anchors.get(grant["id"])
        deadline = self.deadlines.get(grant["id"], 0)
        return (
            bool(anchor)
            and grant["binding"]["owner"] in self.controllers.get(grant["controller"], ())
            and self.monotonic() < deadline
            and abs((self.wall() - anchor[0]) - (self.monotonic() - anchor[1])) <= 5
        )

    def verify(self, principal, ident, binding, action, generation):
        row = self.owned(principal, ident)
        if row["binding"] != binding or action not in row["actions"]:
            raise PermissionError("Resource grant does not authorize this binding or action")
        if row["revoked"] or type(generation) is not int or generation != row["generation"]:
            raise PermissionError("Resource grant was revoked or replaced")
        if self.view(row)["expired"]:
            raise PermissionError("Resource grant expired or requires verification after host restart")
        return row
