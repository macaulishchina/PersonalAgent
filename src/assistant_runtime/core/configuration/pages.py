"""Packaged administrator pages. Discovery never imports plugin runtime code."""

from pathlib import Path, PurePosixPath
import re

SDK_VERSION = 1


def relative_path(value):
    if not isinstance(value, str) or not value or "\\" in value or ":" in value:
        raise ValueError("Invalid packaged resource path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(p in {".", ".."} for p in value.split("/")):
        raise ValueError("Packaged resource escapes its directory")
    return path


def validate_page(page):
    if not isinstance(page, dict) or set(page) - {"version", "root", "entry", "title"}:
        raise ValueError("Invalid management page declaration")
    if type(page.get("version")) is not int or page["version"] != SDK_VERSION:
        raise ValueError("Unsupported management page SDK version")
    relative_path(page.get("root"))
    relative_path(page.get("entry"))
    if not page["entry"].endswith(".html"):
        raise ValueError("Management entry must be HTML")


def package_directory(owner):
    from .catalog import OWNERS, entry_points

    if owner in OWNERS:
        root = Path(__file__).resolve().parents[2]
        return root.joinpath(*OWNERS[owner].removeprefix("assistant_runtime.").split("."))
    matches = [e for e in entry_points(group="personal_agent.plugins") if e.name == owner]
    if len(matches) != 1 or not matches[0].dist:
        raise ValueError("Plugin is not installed or identity is ambiguous")
    entry = matches[0]
    descriptor = entry.value.split(":")[0].replace(".", "/") + "/configuration.json"
    files = [f for f in entry.dist.files or () if str(f) == descriptor]
    if len(files) != 1:
        raise ValueError("Plugin configuration resource is missing")
    return Path(entry.dist.locate_file(files[0])).parent


def asset(owner, page, name):
    validate_page(page)
    root = package_directory(owner)
    directory = root / str(relative_path(page["root"]))
    path = directory / str(relative_path(name))
    # Reject links even when they happen to resolve inside the package today.
    if not path.resolve().is_relative_to(directory.resolve()) or any(
        p.is_symlink() for p in (path, *path.parents)
    ):
        raise ValueError("Invalid management resource")
    if not directory.resolve().is_relative_to(root.resolve()) or not path.is_file():
        raise ValueError("Management resource is missing")
    if path.stat().st_size > 16 * 1024 * 1024:
        raise ValueError("Management resource exceeds 16 MiB")
    return path


def description(owner):
    from .catalog import catalog

    if not isinstance(owner, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", owner):
        raise ValueError("Invalid plugin identity")
    item = next((p for p in catalog()["plugins"] if p["id"] == owner), None)
    if not item or not item.get("configuration") or "error" in item["configuration"]:
        raise ValueError("Plugin configuration unavailable")
    return item["configuration"]


def check():
    """Static CLI/package check, including disabled plugins, without their imports."""
    from .catalog import catalog

    inventory = catalog()
    issues = list(inventory.get("issues", []))
    for item in inventory["plugins"]:
        spec = item.get("configuration") or {}
        page = spec.get("interface", {}).get("page")
        if page:
            try:
                if item["kind"] == "extension" and spec.get("namespace") != "plugin_settings." + item["id"]:
                    raise ValueError("New extension pages must own plugin_settings.<id>")
                asset(item["id"], page, page["entry"])
                directory = package_directory(item["id"]) / page["root"]
                for path in directory.rglob("*"):
                    if path.is_symlink():
                        raise ValueError("Linked page resources are unsupported")
            except (ValueError, OSError) as exc:
                issues.append({"id": item["id"], "error": str(exc)})
    return issues


def main():
    import json

    issues = check()
    print(json.dumps({"ok": not issues, "issues": issues}, ensure_ascii=False, indent=2))
    return int(bool(issues))


if __name__ == "__main__":
    raise SystemExit(main())
