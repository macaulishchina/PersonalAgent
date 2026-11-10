"""One-time explicit composition candidate; never changes a running instance."""

from assistant_runtime.api.system import system

import json
from pathlib import Path
import re
import shutil
import tomllib
from types import SimpleNamespace

from .config import Config, read_config
from .extensions import discover
from assistant_runtime.core.plugin_catalog import OWNERS, declaration
from assistant_runtime.kernel.runtime import Runtime


def set_field(text, key, value):
    before = tomllib.loads(text)
    expected = {**before, key: value}
    assignment = f"{key} = {json.dumps(value, ensure_ascii=False)}"
    if key not in before:
        candidate = assignment + "\n" + text
        if tomllib.loads(candidate) == expected:
            return candidate
    else:
        # Test the parsed result, not a guessed span. This also handles multiline lists
        # with comments without reformatting provider secrets or unrelated settings.
        for match in re.finditer(rf"(?m)^[ \t]*{key}[ \t]*=", text):
            for end in [m.end() for m in re.finditer(r"\n|$", text[match.end() :])]:
                stop = match.end() + end
                candidate = text[: match.start()] + assignment + "\n" + text[stop:]
                try:
                    if tomllib.loads(candidate) == expected:
                        return candidate
                except tomllib.TOMLDecodeError:
                    continue
    raise ValueError(f"Cannot safely replace {key}; prepare it explicitly")


def prepare(source, destination):
    source, destination = Path(source).expanduser().resolve(), Path(destination).expanduser().absolute()
    original = read_config(source)
    current = Config.load(source)
    selected = list(dict.fromkeys([*current.plugins, "conversation", "cli", "execution"]))
    definitions = {}
    pending = list(selected)
    while pending:
        name = pending.pop(0)
        if name in definitions:
            continue
        spec = declaration(name) if name in OWNERS else discover([name], OWNERS)[name]
        definitions[name] = spec
        for dependency in spec.manifest.requires:
            if dependency not in selected:
                selected.append(dependency)
            pending.append(dependency)
    graph = Runtime(None)
    for spec in definitions.values():
        graph.add(SimpleNamespace(manifest=spec.manifest))
    graph.order()  # Includes cycle and version checks, with no plugin construction.
    entry = tomllib.loads(source.read_text(encoding="utf-8"))
    paths = [source, *(source.parent / name for name in entry.get("include", []))]
    if any(path.relative_to(source.parent).parts[0] == "original" for path in paths):
        raise ValueError("The candidate backup name 'original' conflicts with a source include")
    contents = {path.relative_to(source.parent): path.read_text(encoding="utf-8") for path in paths}
    for key, value in (("root", str(current.root)), ("plugins", selected)):
        owner = next(
            (path for path, text in contents.items() if key in tomllib.loads(text)), Path(source.name)
        )
        contents[owner] = set_field(contents[owner], key, value)
    if destination.exists() or destination.is_symlink():
        raise ValueError("Configuration candidate destination must not exist")
    destination.mkdir(parents=True, mode=0o700)
    try:
        for path in paths:
            relative = path.relative_to(source.parent)
            for base, text in (
                (destination, contents[relative]),
                (destination / "original", path.read_text(encoding="utf-8")),
            ):
                output = base / relative
                output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                with output.open("x") as stream:
                    system.private(output)
                    stream.write(text)
        target = destination / source.name
        expected = {**original, "root": str(current.root), "plugins": selected}
        if read_config(target) != expected:
            raise ValueError("Candidate changes unrelated configuration")
        checked = Config.load(target)
        if checked.root != current.root:
            raise ValueError("Candidate moved instance storage")
        return {
            "config": str(target),
            "original": str(destination / "original"),
            "added_plugins": [name for name in selected if name not in current.plugins],
            "root": str(checked.root),
            "applied": False,
        }
    except BaseException:
        shutil.rmtree(destination)
        raise


def migrate_plugins(source, destination):
    """Move bundled settings to their owning namespace in an isolated candidate."""
    import tomlkit

    from assistant_runtime.core.plugin_catalog import metadata

    source = Path(source).expanduser().resolve(strict=True)
    destination = Path(destination).expanduser().absolute()
    before = read_config(source)
    entry = tomllib.loads(source.read_text(encoding="utf-8"))
    root = (source.parent / Path(before.get("root", source.parent)).expanduser()).resolve()
    paths = [source, *(source.parent / name for name in entry.get("include", []))]
    originals = {path.relative_to(source.parent): path.read_text(encoding="utf-8") for path in paths}
    expected = dict(before)
    settings = expected.get("plugin_settings", {})
    if not isinstance(settings, dict):
        raise ValueError("plugin_settings must be a table")
    settings = dict(settings)
    moved = []
    for owner, module in OWNERS.items():
        if owner not in before or not module.startswith("assistant_runtime.plugins."):
            continue
        descriptor = metadata(owner).with_name("configuration.json")
        spec = json.loads(descriptor.read_text(encoding="utf-8"))
        if spec["namespace"] != f"plugin_settings.{owner}":
            continue
        if owner in settings:
            raise ValueError(f"Both old and new settings exist for {owner}")
        settings[owner] = expected.pop(owner)
        moved.append(owner)
    if moved:
        expected["plugin_settings"] = settings
    documents = {}
    for path, original in originals.items():
        text = original
        for owner in moved:
            # Move explicit table headers in place, including disjoint blocks that
            # tomlkit represents as separate pieces of one logical table.
            text = re.sub(
                rf"""(?m)^([ \t]*\[{{1,2}}[ \t]*)({owner}|"{owner}"|'{owner}')(?=[ \t]*[.\]])""",
                lambda match: match[1] + "plugin_settings." + match[2],
                text,
            )
        document = tomlkit.parse(text)
        for owner in moved:
            if owner not in document:
                continue
            previous = document.pop(owner)
            namespaced = document.setdefault("plugin_settings", tomlkit.table())
            if owner in namespaced:
                namespaced[owner].update(previous)
            else:
                namespaced[owner] = previous
        documents[path] = document
    return write_candidate(source, destination, root, originals, documents, expected, migrated_plugins=moved)


def migrate_model_connections(source, destination):
    """Write an explicit preview; leave the source instance and its credentials untouched."""
    import tomlkit

    from assistant_runtime.core.models.migration import consolidate_opencode

    source = Path(source).expanduser().resolve(strict=True)
    destination = Path(destination).expanduser().absolute()
    before = read_config(source)
    registry = before.get("model_api", {})
    converted, migrated, retained = consolidate_opencode(registry)
    expected = {**before, **({"model_api": converted} if "model_api" in before else {})}
    entry = tomllib.loads(source.read_text(encoding="utf-8"))
    root = (source.parent / Path(before.get("root", source.parent)).expanduser()).resolve()
    paths = [source, *(source.parent / name for name in entry.get("include", []))]
    originals = {path.relative_to(source.parent): path.read_text(encoding="utf-8") for path in paths}
    documents = {path: tomlkit.parse(text) for path, text in originals.items()}

    for group in migrated:
        owner = None
        for document in documents.values():
            settings = document.get("model_api", {})
            providers = settings.get("providers", {})
            for ident in group["replaces"]:
                if ident in providers:
                    if owner is None:
                        owner = providers
                    del providers[ident]
            for alias in group["models"]:
                if alias in settings.get("entries", {}):
                    model = settings["entries"][alias]
                    model["provider"] = group["connection"]
                    model["protocol"] = converted["entries"][alias]["protocol"]
        if owner is None:
            raise ValueError("Could not locate original connection configuration")
        owner[group["connection"]] = converted["providers"][group["connection"]]
    return write_candidate(
        source,
        destination,
        root,
        originals,
        documents,
        expected,
        merged_connections=migrated,
        retained=retained,
    )


def write_candidate(source, destination, root, originals, documents, expected, **report):
    import tomlkit

    # Keep relative includes intact while retaining the source instance storage.
    expected["root"] = str(root)
    for path, document in documents.items():
        if path != Path(source.name) and "root" in document:
            del document["root"]
    documents[Path(source.name)]["root"] = str(root)
    if any(path.parts[0] == "original" for path in originals):
        raise ValueError("The candidate backup name 'original' conflicts with a source include")
    destination.mkdir(parents=True, mode=0o700, exist_ok=False)
    try:
        system.private(destination, directory=True)
        for path, document in documents.items():
            for base, text in (
                (destination, tomlkit.dumps(document)),
                (destination / "original", originals[path]),
            ):
                output = base / path
                output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                with output.open("x", encoding="utf-8") as stream:
                    system.private(output)
                    stream.write(text)
        target = destination / source.name
        if read_config(target) != expected:
            raise ValueError("Candidate changes unrelated configuration")
        from assistant_runtime.core.configuration.validation import validate_candidate

        validate_candidate(
            source.name,
            {path.as_posix(): (destination / path).read_bytes() for path in documents},
            root=root,
            entry_directory=destination,
        )
        return {
            "config": str(target),
            "original": str(destination / "original"),
            **report,
            "root": str(root),
            "applied": False,
        }
    except BaseException:
        shutil.rmtree(destination)
        raise
