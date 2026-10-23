"""Resource discovery follows enabled owners, never filesystem skill scanning."""

from .extensions import discover

from assistant_runtime.core.plugin_catalog import OWNERS, declaration

BUILTINS = frozenset(OWNERS)


def skill_paths(enabled):
    paths = []
    for name in enabled:
        if name not in BUILTINS:
            definition = discover([name], BUILTINS)[name]
            selected = definition.skills
        else:
            selected = declaration(name).skills
        for path in selected:
            if not (path / "SKILL.md").is_file():
                raise ValueError("Bundled assistant skill is missing: " + path.name)
            if path.name in {p.name for p in paths}:
                raise ValueError("Duplicate plugin skill: " + path.name)
            paths.append(path)
    return tuple(paths)
