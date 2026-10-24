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
    selected = list(dict.fromkeys([*current.plugins, "conversation", "execution"]))
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
