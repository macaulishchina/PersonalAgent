"""Declared field patches; defaults are presentation, never implicit file writes."""

from copy import deepcopy
import tomllib
from .catalog import specifications

MISSING = object()


def lookup(value, path, default=MISSING):
    for key in path:
        if not isinstance(value, dict) or key not in value:
            return default
        value = value[key]
    return value


def described_schema(spec):
    from .schema import expand

    schema = expand(spec["schema"])
    for annotation, paths in (
        ("writeOnly", spec.get("sensitive", [])),
        ("readOnly", spec.get("read_only", [])),
    ):
        for path in paths:
            node = schema
            for component in path.split("."):
                node = (
                    node.setdefault("additionalProperties", {})
                    if component == "*"
                    else node.setdefault("properties", {}).setdefault(component, {})
                )
            node[annotation] = True
    return schema


def declared_fields(inventory=None):
    from .catalog import catalog
    from .interface import field_interface

    inventory = inventory or catalog()
    fields = []
    for spec in specifications(inventory):
        namespace = spec.get("namespace", "")
        prefix = namespace.split(".") if namespace else []
        schema = described_schema(spec)
        properties = schema.get("properties", {})
        if prefix and not properties and isinstance(schema.get("additionalProperties"), dict):
            properties = {"": {**schema, "title": schema.get("title", spec.get("title", namespace))}}
        for key, rule in properties.items():
            if rule is False:
                continue
            rule = rule if isinstance(rule, dict) else {}
            fields.append(
                {
                    **field_interface(spec, key, rule, inventory),
                    "path": [*prefix, key] if key else prefix,
                    "schema": rule,
                    "group": rule.get("x-group", spec.get("group", "extensions")),
                    "owner": spec.get("title", namespace),
                }
            )
    return fields


def rule_for(path, fields):
    for field in fields:
        prefix = field["path"]
        if path[: len(prefix)] != prefix:
            continue
        rule = field["schema"]
        locked = rule.get("readOnly", False)
        for key in path[len(prefix) :]:
            rule = rule.get("properties", {}).get(key, rule.get("additionalProperties"))
            if not isinstance(rule, dict):
                raise ValueError("Field is not declared")
            locked = locked or rule.get("readOnly", False)
        return {**rule, "readOnly": locked}
    raise ValueError("Field is not declared")


def secret_shape(rule):
    if not isinstance(rule, dict):
        return False
    return bool(
        rule.get("writeOnly")
        or rule.get("x-sensitive")
        or any(secret_shape(r) for r in rule.get("properties", {}).values())
        or (isinstance(rule.get("additionalProperties"), dict) and secret_shape(rule["additionalProperties"]))
        or (isinstance(rule.get("items"), dict) and secret_shape(rule["items"]))
    )


def public_rule(rule):
    if not isinstance(rule, dict):
        return rule
    rule = deepcopy(rule)
    if rule.get("writeOnly") or rule.get("x-sensitive"):
        for key in ("default", "examples", "enum", "const"):
            rule.pop(key, None)
    for key in ("properties", "$defs"):
        if key in rule:
            rule[key] = {name: public_rule(child) for name, child in rule[key].items()}
    for key in ("items", "additionalProperties", "if", "then", "else"):
        if isinstance(rule.get(key), dict):
            rule[key] = public_rule(rule[key])
    for key in ("allOf", "anyOf", "oneOf"):
        if key in rule:
            rule[key] = [public_rule(child) for child in rule[key]]
    if secret_shape(rule):
        rule.pop("default", None)
    return rule


def redact_declared(value, rule):
    rule = rule if isinstance(rule, dict) else {}
    if rule.get("writeOnly") or rule.get("x-sensitive"):
        return {"configured": value not in (None, "")}
    if isinstance(value, dict):
        return {
            k: redact_declared(
                v, rule.get("properties", {}).get(k, rule.get("additionalProperties", {})) or {}
            )
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [redact_declared(v, rule.get("items", {})) for v in value]
    return value


def present(snapshot, entry_name, inventory=None, *, root=None):
    from .files import redact

    documents = {n: tomllib.loads(raw.decode()) for n, raw in snapshot.contents.items()}
    result = []
    for field in declared_fields(inventory):
        matches = [(n, lookup(doc, field["path"])) for n, doc in documents.items()]
        matches = [(n, v) for n, v in matches if v is not MISSING]
        value = MISSING
        origins = []
        for name, found in matches:
            origins.append(name)
            if value is MISSING:
                value = deepcopy(found)
            elif isinstance(value, dict) and isinstance(found, dict):
                value = merge(value, found)
            else:
                raise ValueError("Duplicate configuration field")
        explicit = value is not MISSING
        if not explicit:
            value = field["schema"].get("default", MISSING)
            if field["path"] == ["workspace"] and root is not None:
                value = str(root)
        item = {
            **field,
            "explicit": explicit,
            "files": origins,
            "source": "file" if explicit else "default",
            "schema": public_rule(field["schema"]),
        }
        if value is not MISSING:
            item["value"] = redact(redact_declared(value, field["schema"]))
        result.append(item)
    return {"version": snapshot.version, "fields": result, "entry": entry_name}


def merge(a, b):
    for key, value in b.items():
        if key not in a:
            a[key] = deepcopy(value)
        elif isinstance(a[key], dict) and isinstance(value, dict):
            merge(a[key], value)
        else:
            raise ValueError("Duplicate configuration field")
    return a


def patch_documents(snapshot, entry_name, patches, inventory=None):
    if not isinstance(patches, list) or not 0 < len(patches) <= 512:
        raise ValueError("Expected bounded field patches")
    fields = declared_fields(inventory)
    documents = {n: tomllib.loads(raw.decode()) for n, raw in snapshot.contents.items()}
    seen = []
    for patch in patches:
        if not isinstance(patch, dict) or set(patch) - {"path", "op", "value"}:
            raise ValueError("Invalid field operation")
        path = patch.get("path")
        if (
            not isinstance(path, list)
            or not path
            or len(path) > 24
            or any(not isinstance(k, str) or not k or len(k) > 128 for k in path)
        ):
            raise ValueError("Invalid field path")
        if any(
            (path[: len(old)] == old or old[: len(path)] == path)
            and not (op in {"create", "default"} and len(old) < len(path) and path[: len(old)] == old)
            for old, op in seen
        ):
            raise ValueError("Overlapping patches")
        seen.append((path, patch.get("op")))
        rule = rule_for(path, fields)
        if rule.get("readOnly"):
            raise ValueError("Field requires local maintenance")
        operation = patch.get("op")
        secret = rule.get("writeOnly") or rule.get("x-sensitive")
        if operation not in ({"replace_secret", "clear_secret"} if secret else {"set", "default", "create"}):
            raise ValueError("Invalid field operation")
        if not secret and operation == "set" and secret_shape(rule):
            raise ValueError("Edit sensitive child fields individually")
        matches = [n for n, doc in documents.items() if lookup(doc, path) is not MISSING]
        if operation == "create":
            if rule.get("type") != "object" or patch.get("value") != {} or matches:
                raise ValueError("Create requires a new empty declared object")
        if len(matches) > 1:
            # A table may be split across includes. Edit children rather than relocating it.
            raise ValueError("Field spans multiple files; edit individual child fields")
        if operation in {"default", "clear_secret"}:
            for name in matches:
                del lookup(documents[name], path[:-1])[path[-1]]
            continue
        if "value" not in patch or patch["value"] is None:
            raise ValueError("TOML field requires a value")
        if not matches:
            for depth in range(len(path) - 1, 0, -1):
                matches = [n for n, doc in documents.items() if isinstance(lookup(doc, path[:depth]), dict)]
                if matches:
                    break
        name = matches[0] if len(matches) == 1 else entry_name
        target = documents[name]
        for key in path[:-1]:
            target = target.setdefault(key, {})
            if not isinstance(target, dict):
                raise ValueError("Field parent is not a table")
        target[path[-1]] = deepcopy(patch["value"])
    return documents
