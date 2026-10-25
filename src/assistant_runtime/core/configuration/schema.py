"""Shared schema validation and explicit defaults for plugin configuration."""

from copy import deepcopy
from functools import lru_cache
import json
from jsonschema import Draft202012Validator
from assistant_runtime.api.configuration import ConfigurationError


@lru_cache(maxsize=128)
def validator(encoded):
    schema = json.loads(encoded)

    def local_references(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if key in {"$ref", "$dynamicRef"} and (
                    not isinstance(value, str) or not value.startswith("#")
                ):
                    raise ValueError("Configuration schemas must use local references")
                local_references(value)
        elif isinstance(node, list):
            for value in node:
                local_references(value)

    local_references(schema)
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def expand(schema):
    """Bounded local references for shared defaults, redaction and field navigation."""
    root = deepcopy(schema)

    def visit(value, seen=(), depth=0):
        if depth > 48:
            raise ValueError("Configuration schema nesting exceeds bound")
        if isinstance(value, list):
            return [visit(child, seen, depth + 1) for child in value]
        if not isinstance(value, dict):
            return value
        reference = value.get("$ref")
        if reference is not None:
            if not reference.startswith("#/") or reference in seen:
                raise ValueError("Configuration schemas require acyclic local references")
            target = root
            for key in reference[2:].split("/"):
                target = target[key.replace("~1", "/").replace("~0", "~")]
            merged = {
                **visit(target, (*seen, reference), depth + 1),
                **{k: v for k, v in value.items() if k != "$ref"},
            }
            return visit(merged, (*seen, reference), depth + 1)
        return {key: visit(child, seen, depth + 1) for key, child in value.items()}

    return visit(root)


def resolve(spec, raw):
    if spec.version != 1:
        raise ValueError("Unsupported configuration specification version")
    schema = expand(dict(spec.schema))
    checked = validator(json.dumps(schema, sort_keys=True))
    value = deepcopy(raw)

    def defaults(node, shape):
        if not isinstance(shape, dict):
            return
        if isinstance(node, dict):
            for key, rule in shape.get("properties", {}).items():
                if not isinstance(rule, dict):
                    continue
                if key not in node and "default" in rule:
                    node[key] = deepcopy(rule["default"])
                if key in node:
                    defaults(node[key], rule)
            additional = shape.get("additionalProperties")
            if isinstance(additional, dict):
                for key in node.keys() - shape.get("properties", {}).keys():
                    defaults(node[key], additional)
        elif isinstance(node, list) and isinstance(shape.get("items"), dict):
            for item in node:
                defaults(item, shape["items"])
        # Conditional defaults must be unambiguous and use only already resolved data.
        for branch in shape.get("allOf", []):
            defaults(node, branch)
        if "if" in shape:
            branch = "then" if Draft202012Validator(shape["if"]).is_valid(node) else "else"
            defaults(node, shape.get(branch, {}))

    defaults(value, schema)
    errors = sorted(checked.iter_errors(value), key=lambda e: str(list(e.path)))
    if errors:
        # jsonschema's default message may include the actual secret value.
        error = errors[0]
        messages = {
            "type": "类型不正确",
            "required": "缺少必填项",
            "minimum": "低于允许的最小值",
            "maximum": "超过允许的最大值",
            "enum": "请选择可用选项",
            "additionalProperties": "包含未声明的配置项",
            "minLength": "内容过短",
            "maxLength": "内容过长",
            "pattern": "格式不符合要求",
        }
        message = messages.get(error.validator, "配置不符合声明规则")
        if "propertyNames" in error.absolute_schema_path:
            message = error.schema.get("description", "条目名称不符合格式要求，请检查条目名称。")
        raise ConfigurationError(
            [*(spec.namespace.split(".") if spec.namespace else []), *error.absolute_path],
            str(error.validator),
            message,
        )
    return value


def validate_structure(spec, raw):
    """Dormant settings keep their types without requiring usable connections or ranges."""

    def shape(rule):
        result = {k: rule[k] for k in ("type",) if k in rule}
        if "properties" in rule:
            result["properties"] = {k: shape(v) for k, v in rule["properties"].items()}
        if isinstance(rule.get("items"), dict):
            result["items"] = shape(rule["items"])
        if isinstance(rule.get("additionalProperties"), dict):
            result["additionalProperties"] = shape(rule["additionalProperties"])
        return result

    errors = list(Draft202012Validator(shape(expand(dict(spec.schema)))).iter_errors(raw))
    if errors:
        raise ConfigurationError(
            [*(spec.namespace.split(".") if spec.namespace else []), *errors[0].absolute_path],
            "type",
            "配置类型不正确",
        )


def resolve_definition(definition, raw):
    value = resolve(definition.configuration, raw) if definition.configuration else deepcopy(raw)
    return definition.validate(value) if definition.validate else value
