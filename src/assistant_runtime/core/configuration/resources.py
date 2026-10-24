"""Registered instance resources. No arbitrary filesystem editing or OAuth token backup."""

from dataclasses import dataclass
from pathlib import Path
import json
import os
import re
import tomllib


@dataclass(frozen=True)
class Resource:
    id: str
    path: Path
    kind: str
    title: str
    sensitive: bool = False
    read_only: bool = False
    schema: dict | None = None


class Resources:
    def __init__(self, root):
        self.root = Path(root).resolve()

    def registered(self, contents):
        from .fields import merge

        data = {}
        for raw in contents.values():
            merge(data, tomllib.loads(raw.decode()))
        result = [
            Resource("credentials", self.root / ".env", "dotenv", "私有环境凭据", True),
            Resource(
                "gateway_key",
                self.root / "state/model-api/gateway.key",
                "credential",
                "模型网关私有口令",
                True,
            ),
            Resource("management", self.root / "management.toml", "management", "管理服务地址与生命周期"),
            Resource(
                "mcp",
                Path(
                    data.get("external_mcp", {}).get("registry_path") or self.root / "config/mcp/servers.json"
                ),
                "mcp",
                "外部 MCP 注册表",
                True,
            ),
            Resource(
                "web_access",
                Path(data.get("web", {}).get("token_file") or self.root / "state/web-access-token"),
                "credential",
                "聊天网页访问口令",
                True,
            ),
        ]
        for name in data.get("instance_skills", []):
            if not re.fullmatch("[a-z][a-z0-9-]{0,63}", name):
                raise ValueError("Invalid instance skill")
            skill = self.root / "config/skills" / name
            result.append(Resource("skill:" + name, skill / "SKILL.md", "skill", name))
            if skill.exists():
                assets = sorted(skill.rglob("*"))
                if len(assets) > 128:
                    raise ValueError("Instance skill has too many resources")
                for asset in assets:
                    if asset.is_symlink():
                        raise ValueError("Instance skill symlinks are unsupported")
                    if asset.is_file() and asset.name != "SKILL.md":
                        relative = asset.relative_to(skill).as_posix()
                        result.append(Resource("asset:" + name + ":" + relative, asset, "asset", relative))
        # External descriptions can register static text/JSON resources inside their private config area.
        from .catalog import installed_descriptions

        for item in installed_descriptions():
            if item["id"] not in set(data.get("plugins", [])) | set(data.get("plugin_settings", {})):
                continue
            if not re.fullmatch("[a-z][a-z0-9_]{0,63}", item["id"]):
                raise ValueError("Invalid installed plugin resource owner")
            description = item.get("configuration") or {}
            for spec in description.get("resources", []):
                if not isinstance(spec, dict) or spec.get("kind") not in {"text", "json", "credential"}:
                    raise ValueError("Unsupported plugin configuration resource")
                name = spec.get("id", "")
                if not re.fullmatch("[a-z][a-z0-9_-]{0,63}", name):
                    raise ValueError("Invalid resource identity")
                relative = Path(spec.get("path", ""))
                base = self.root / "config/plugins" / item["id"]
                path = base / relative
                if relative.is_absolute() or not path.resolve().is_relative_to(base):
                    raise ValueError("Plugin resource escapes private configuration")
                result.append(
                    Resource(
                        item["id"] + ":" + name,
                        path,
                        spec["kind"],
                        spec.get("title", name),
                        spec.get("sensitive", False),
                        schema=spec.get("schema"),
                    )
                )
        return {
            r.id: Resource(
                r.id,
                r.path,
                r.kind,
                r.title,
                r.sensitive,
                not r.path.is_absolute() or not r.path.resolve().is_relative_to(self.root),
                r.schema,
            )
            for r in result
        }

    def path(self, resource):
        path = resource.path
        if (
            resource.read_only
            or not path.resolve().is_relative_to(self.root)
            or any(p.is_symlink() for p in [path, *path.parents])
        ):
            raise ValueError("Resource requires local maintenance")
        return path

    def snapshot(self, contents):
        result = {}
        for ident, resource in self.registered(contents).items():
            if resource.read_only:
                continue
            path = self.path(resource)
            raw = path.read_bytes() if path.is_file() else None
            if raw is not None and len(raw) > 1024 * 1024:
                raise ValueError("Configuration resource exceeds 1 MiB")
            result[ident] = raw
        if (
            len(result) > 256
            or sum(len(raw) for raw in result.values() if raw is not None) > 16 * 1024 * 1024
        ):
            raise ValueError("Configuration resources exceed aggregate bounds")
        return result

    def validate(self, resource, raw):
        if raw is None:
            if resource.kind == "management":
                raise ValueError("Management bootstrap cannot be removed")
            return
        if not isinstance(raw, bytes) or len(raw) > 1024 * 1024:
            raise ValueError("Resource exceeds bound")
        if resource.kind == "asset":
            return
        text = raw.decode()
        if resource.kind == "dotenv":
            from dotenv import dotenv_values
            from io import StringIO

            # Credentials are literal strings, never shell interpolation.
            if any(
                line.strip()
                and not line.lstrip().startswith("#")
                and not re.match(r"^(?:export )?[A-Za-z_][A-Za-z0-9_]*\s*=", line)
                for line in text.splitlines()
            ):
                raise ValueError("Invalid dotenv assignment")
            values = dotenv_values(stream=StringIO(text), interpolate=False)
            if any(v is None or "\n" in v or "\r" in v for v in values.values()):
                raise ValueError("Invalid credential value")
        elif resource.kind == "mcp":
            from assistant_runtime.core.integrations.configuration import validate_registry

            validate_registry(json.loads(text))
        elif resource.kind == "json":
            value = json.loads(text)
            if resource.schema:
                from .schema import resolve
                from assistant_runtime.api.configuration import ConfigurationSpec

                resolve(ConfigurationSpec(schema=resource.schema), value)
        elif resource.kind == "management":
            from .settings import ManagementSettings

            data = tomllib.loads(text)
            original = resource.path
            if original.exists():
                before = tomllib.loads(original.read_text(encoding="utf-8"))
                if any(data.get(key) != before.get(key) for key in ("root", "entry")):
                    raise ValueError("Bootstrap root and entry require local migration")
            if Path(data.get("root", "")).resolve() != self.root:
                raise ValueError("Instance root migration is not an online operation")
            # Validate independently without replacing a live bootstrap.
            import tempfile

            with tempfile.TemporaryDirectory() as folder:
                candidate = Path(folder) / original.name
                candidate.write_text(text, encoding="utf-8")
                settings = ManagementSettings.load(candidate)
                if not settings.entry.is_relative_to(self.root):
                    raise ValueError("Configuration entry migration requires local maintenance")
        elif resource.kind == "credential":
            if (
                len(text.strip()) < (8 if resource.id == "web_access" else 24)
                or len(text) > 4096
                or any(c.isspace() for c in text.strip())
            ):
                raise ValueError("Access credentials require at least 24 non-whitespace characters")
        elif resource.kind == "skill":
            if not text.strip() or len(raw) > 128 * 1024:
                raise ValueError("Skill must be nonempty and at most 128 KiB")

    def public(self, contents, values):
        result = []
        for ident, resource in self.registered(contents).items():
            raw = values.get(ident)
            item = {
                "id": ident,
                "title": resource.title,
                "kind": resource.kind,
                "file": str(resource.path),
                "read_only": resource.read_only,
                "configured": raw is not None,
                "sensitive": resource.sensitive,
            }
            if raw is not None and not resource.sensitive and resource.kind != "asset":
                item["value"] = raw.decode()
            if resource.kind == "dotenv":
                from dotenv import dotenv_values
                from io import StringIO

                configured = dotenv_values(stream=StringIO((raw or b"").decode()), interpolate=False)
                item["keys"] = [
                    {
                        "name": key,
                        "configured": bool(configured.get(key)),
                        "source": "environment" if key in os.environ else "file",
                        "environment_configured": bool(os.environ.get(key)),
                    }
                    for key in sorted(set(configured) | self.credential_names(contents))
                ]
            result.append(item)
        return result

    def credential_names(self, contents):
        data = {}
        from .fields import merge

        for raw in contents.values():
            merge(data, tomllib.loads(raw.decode()))
        names = set()

        def visit(value):
            if isinstance(value, dict):
                for key, child in value.items():
                    if (
                        (key.endswith("_env") or key == "env_vars")
                        and isinstance(child, str)
                        and re.fullmatch("[A-Z_][A-Z0-9_]*", child)
                    ):
                        names.add(child)
                    else:
                        visit(child)
            elif isinstance(value, list):
                for child in value:
                    visit(child)

        visit(data)
        from assistant_runtime.core.models.settings import PRESETS

        for p in data.get("model_api", {}).get("providers", {}).values():
            if p.get("preset") in PRESETS:
                names.add(p.get("key_env", PRESETS[p["preset"]]["key_env"]))
        return names

    def frozen_target(self, folder, resource):
        if resource.kind in {"skill", "asset"}:
            return folder / "resources/skills" / resource.path.relative_to(self.root / "config/skills")
        return folder / "resources" / resource.id

    def provision(self, contents):
        # Provision the existing web owner's access token once before freezing a generation.
        from .fields import merge

        data = {}
        for raw in contents.values():
            merge(data, tomllib.loads(raw.decode()))
        if "web" in data.get("plugins", []) and data.get("web", {}).get("enable", True):
            resource = self.registered(contents)["web_access"]
            if not resource.read_only:
                path = self.path(resource)
                if not path.exists():
                    import secrets
                    from .files import atomic

                    atomic(path, (secrets.token_urlsafe(32) + "\n").encode())

    def prepare_defaults(self, contents, values, touched):
        from .fields import merge

        data = {}
        for raw in contents.values():
            merge(data, tomllib.loads(raw.decode()))
        if (
            "web" in data.get("plugins", [])
            and data.get("web", {}).get("enable", True)
            and values.get("web_access") is None
        ):
            if "web_access" in touched:
                raise ValueError("An enabled web entry requires an access credential")
            resource = self.registered(contents)["web_access"]
            if not resource.read_only:
                import secrets

                values["web_access"] = (secrets.token_urlsafe(32) + "\n").encode()
                touched.add("web_access")

    def validate_candidate(self, config, contents, values):
        from .validation import validate_ports
        from .settings import ManagementSettings
        import tempfile

        token = values.get("web_access")
        if token is not None and len(token.strip()) < 24:
            import hashlib
            import hmac

            fingerprint = config.web.get("approved_short_password_sha256", "")
            if not hmac.compare_digest(hashlib.sha256(token.strip()).hexdigest(), fingerprint):
                raise ValueError("Short web credential is not explicitly approved")
        raw = values.get("management")
        if raw is not None:
            with tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / "management.toml"
                path.write_bytes(raw)
                validate_ports(config, ManagementSettings.load(path))
        if values.get("mcp") is not None:
            from assistant_runtime.core.integrations.configuration import validate_registry

            validate_registry(json.loads(values["mcp"]), config.external_mcp.get("max_servers", 32))

    def prepare(self, resource, previous, operation):
        if resource.read_only or resource.kind == "asset":
            raise ValueError("Resource is read-only")
        op = operation.get("op")
        if resource.kind == "dotenv":
            from dotenv import set_key, unset_key
            import tempfile

            key = operation.get("key")
            if not isinstance(key, str) or not re.fullmatch("[A-Za-z_][A-Za-z0-9_]{0,127}", key):
                raise ValueError("Invalid credential name")
            if op not in {"replace_secret", "clear_secret"}:
                raise ValueError("Explicit credential operation required")
            with tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / ".env"
                path.write_bytes(previous or b"")
                if op == "clear_secret":
                    unset_key(path, key)
                else:
                    value = operation.get("value")
                    if (
                        not isinstance(value, str)
                        or not value
                        or len(value) > 32768
                        or any(c in value for c in "\r\n\0")
                    ):
                        raise ValueError("Invalid credential value")
                    set_key(path, key, value, quote_mode="always")
                return path.read_bytes()
        if resource.sensitive:
            if op == "clear_secret":
                return None
            if op != "replace_secret":
                raise ValueError("Explicit secret operation required")
        elif op != "set":
            raise ValueError("Resource requires set operation")
        value = operation.get("value")
        if not isinstance(value, str):
            raise ValueError("Resource requires text")
        if resource.kind == "mcp":
            from assistant_runtime.core.integrations.configuration import prepare_registry

            return json.dumps(
                prepare_registry(
                    json.loads((previous or b'{"revision":0,"servers":{},"removed":[]}').decode()),
                    json.loads(value),
                ),
                ensure_ascii=False,
                indent=2,
            ).encode()
        return value.encode()
