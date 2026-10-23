"""Explicit instance skill selection; never discover Coding AI project resources."""

from pathlib import Path
import re


def validate_names(names):
    if (
        not isinstance(names, list)
        or len(names) > 32
        or any(not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", name) for name in names)
        or len(set(names)) != len(names)
    ):
        raise ValueError("instance_skills must be a unique list of at most 32 skill names")


def instance_skill_paths(root, names):
    validate_names(names)
    base = Path(root).resolve() / "config/skills"
    paths = []
    for name in names:
        path = (base / name).resolve()
        entry = path / "SKILL.md"
        if not path.is_relative_to(base) or not entry.resolve().is_relative_to(base) or not entry.is_file():
            raise ValueError(f"Instance skill must exist inside config/skills: {name}")
        paths.append(path)
    return tuple(paths)
