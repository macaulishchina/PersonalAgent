"""Registered instance resources. No arbitrary filesystem editing or OAuth token backup."""

from assistant_runtime.api.configuration import ConfigurationResource as Resource
from pathlib import Path
import json
import os
import re
import tomllib


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
            Resource("management", self.root / "management.toml", "management", "管理服务地址与生命周期"),
        ]
        from assistant_runtime.core.integrations.configuration_resources import (
            registered as integration_resources,
        )
        from assistant_runtime.plugins.web.configuration_resources import registered as web_resources

        result.extend(integration_resources(self.root, data))
        result.extend(web_resources(self.root, data.get("web", {})))
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
            if resource.id == "web_access":
                from assistant_runtime.api.passwords import validate_password

                validate_password(text.rstrip("\r\n"))
            elif len(text.strip()) < 24 or len(text) > 4096 or any(c.isspace() for c in text.strip()):
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
        from assistant_runtime.core.models.configuration_resources import credential_names

        names.update(credential_names(data))
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
        from assistant_runtime.plugins.web.configuration_resources import provision

        resource = self.registered(contents)["web_access"]
        if not resource.read_only:
            provision(self.path(resource), data.get("web", {}), "web" in data.get("plugins", []))

    def prepare_defaults(self, contents, values, touched):
        from .fields import merge

        data = {}
        for raw in contents.values():
            merge(data, tomllib.loads(raw.decode()))
        from assistant_runtime.plugins.web.configuration_resources import prepare_defaults

        resource = self.registered(contents)["web_access"]
        value = prepare_defaults(
            values.get("web_access"),
            "web_access" in touched,
            not resource.read_only,
            data.get("web", {}),
            "web" in data.get("plugins", []),
        )
        if value != values.get("web_access"):
            values["web_access"] = value
            touched.add("web_access")

    def validate_candidate(self, config, contents, values):
        from .validation import validate_ports
        from .settings import ManagementSettings
        import tempfile

        from assistant_runtime.plugins.web.configuration_resources import validate_candidate as validate_web
        from assistant_runtime.core.integrations.configuration_resources import (
            validate_candidate as validate_integrations,
        )

        validate_web(config.web, "web" in config.enabled_plugins, values.get("web_access"))
        validate_integrations(config, values)
        raw = values.get("management")
        if raw is not None:
            with tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / "management.toml"
                path.write_bytes(raw)
                validate_ports(config, ManagementSettings.load(path))

    def audit_fields(self, resource, operation):
        if resource.kind == "mcp":
            from assistant_runtime.core.integrations.configuration import audit_fields

            return audit_fields(operation)
        return []

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
        if resource.kind == "mcp" and op == "patch":
            from assistant_runtime.core.integrations.configuration import patch_registry

            return json.dumps(
                patch_registry(
                    json.loads((previous or b'{"revision":0,"servers":{},"removed":[]}').decode()),
                    operation.get("value"),
                ),
                ensure_ascii=False,
                indent=2,
            ).encode()
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
