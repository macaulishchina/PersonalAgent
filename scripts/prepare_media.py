"""Explicit Aliyun onboarding. Run before enabling media, or with its service stopped.

Uses the same project environment and trusted provider. Never reads an OpenList DB.
Creates/reuses only 影视/电影 and 影视/电视剧; never overwrites user files.
"""

import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import re

from assistant_runtime.config import Config
from assistant_runtime.plugins.media.aliyun import Aliyun
from assistant_runtime.plugins.media.errors import MediaError
from assistant_runtime.plugins.media.private import save


async def children(provider, drive, folder):
    marker, seen, result = "", set(), []
    for _ in range(10):
        page = await provider.call("list", {"drive_id": drive, "folder_id": folder, "marker": marker})
        result.extend(page["items"])
        marker = page.get("marker")
        if not marker:
            return result
        if marker in seen:
            break
        seen.add(marker)
    raise MediaError("onboarding_listing_limit")


async def folder(provider, drive, parent, name):
    async def find():
        matches = [x for x in await children(provider, drive, parent) if x.get("name") == name]
        if len(matches) > 1 or (matches and matches[0].get("type") != "folder"):
            raise MediaError("onboarding_folder_conflict")
        return matches[0]["file_id"] if matches else None

    existing = await find()
    if existing:
        return existing
    try:
        result = await provider.call("mkdir", {"drive_id": drive, "folder_id": parent, "name": name})
        if result.get("type") == "folder" and result.get("file_id"):
            return result["file_id"]
    except MediaError as exc:
        if not exc.uncertain:
            raise
    # A lost reply does not justify issuing create again.
    recovered = await find()
    if not recovered:
        raise MediaError("onboarding_create_unconfirmed")
    return recovered


async def prepare(args):
    config = Config.load(args.config)
    root = config.state_dir / "media"
    provider = Aliyun(root, lambda state: None)
    try:
        provider.claim()
        if args.import_aliyun_auth:
            destination = provider.credentials / "account.json"
            if destination.exists():
                raise ValueError("Credentials already exist; use the plugin login flow to replace them")
            token = json.loads(args.import_aliyun_auth.read_text())
            if not all(
                isinstance(token.get(k), str) and token[k]
                for k in ("refresh_token", "access_token", "user_id")
            ):
                raise ValueError("Expected an aligo Web auth file, not an OpenList OAuth token")
            save(
                provider.credentials / "identity.json",
                {"identity": hashlib.sha256(token["user_id"].encode()).hexdigest()},
            )
            save(destination, token)
        await provider.call("status")
        drives = await provider.call("drives")
        matches = [d for d in drives["items"] if d.get("drive_name") == "resource"]
        if len(matches) != 1:
            raise ValueError("A unique Aliyun resource drive is required")
        drive = matches[0]["drive_id"]
        if not args.create_targets:
            print("Account verified. Use --create-targets to create/reuse 影视/电影 and 影视/电视剧.")
            return
        parent = await folder(provider, drive, "root", "影视")
        targets = {}
        for ident, name in (("movies", "电影"), ("series", "电视剧")):
            ident_folder = await folder(provider, drive, parent, name)
            targets[ident] = {
                "drive_id": drive,
                "folder_id": ident_folder,
                "openlist_path": args.mount_path.rstrip("/") + "/影视/" + name,
            }
        save(root / "targets.json", targets)
        snippet = "\n[media]\nsearch_url = " + json.dumps(args.search_url) + "\n"
        if args.openlist_url:
            snippet += "openlist_url = " + json.dumps(args.openlist_url) + "\n"
        for ident, target in targets.items():
            snippet += "\n[media.targets." + ident + "]\n"
            snippet += "".join(
                key + " = " + json.dumps(value, ensure_ascii=False) + "\n" for key, value in target.items()
            )
        if args.apply_config:
            path = args.config.resolve()
            content = path.read_text()
            if config.media:
                raise ValueError(
                    "Media is already configured; merge targets explicitly instead of replacing it"
                )
            save(root / "config-before-media.json", {"content": content})
            plugins = list(config.plugins)
            if "media" not in plugins:
                plugins.append("media")
            replacement = "plugins = " + json.dumps(plugins)
            pattern = r"(?ms)^plugins\s*=\s*\[.*?\]"
            if re.search(pattern, content):
                content = re.sub(pattern, lambda match: replacement, content, count=1)
            else:
                content = replacement + "\n" + content
            candidate = content + snippet
            import tomllib

            Config(root=config.root, **{k: v for k, v in tomllib.loads(candidate).items() if k != "root"})
            # Preserve all unrelated settings; private backup made above.
            from tempfile import NamedTemporaryFile
            import os

            with NamedTemporaryFile(
                mode="w", dir=path.parent, prefix=".media-config-", delete=False
            ) as stream:
                temporary = Path(stream.name)
                os.fchmod(stream.fileno(), 0o600)
                stream.write(candidate)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            print("Media configured; restart only after the full project checks pass.")
        else:
            print(snippet)
    finally:
        await provider.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--import-aliyun-auth", type=Path)
    parser.add_argument("--create-targets", action="store_true")
    parser.add_argument("--apply-config", action="store_true")
    parser.add_argument("--search-url", default="http://127.0.0.1:8888")
    parser.add_argument("--openlist-url", default="")
    parser.add_argument("--mount-path", default="/阿里云盘")
    args = parser.parse_args()
    if args.apply_config and not args.create_targets:
        parser.error("--apply-config requires --create-targets")
    asyncio.run(prepare(args))


if __name__ == "__main__":
    main()
