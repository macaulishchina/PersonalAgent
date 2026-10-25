"""Install the checkout with remembered dependency choices; never manage instances."""

import argparse
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import tomllib

ROOT = Path(__file__).resolve().parents[1]


def plan(root, args, environment, platform):
    if platform not in {"win32", "linux"}:
        raise ValueError("PA installation supports Windows/Windows Server and Linux")
    target = Path(environment.get("UV_PROJECT_ENVIRONMENT") or root / ".venv").expanduser()
    target = (root / target).resolve() if not target.is_absolute() else target.resolve()
    receipt = target / "pa-install.json"
    previous = {}
    if receipt.exists():
        previous = json.loads(receipt.read_text(encoding="utf-8"))
        if (
            not isinstance(previous, dict)
            or type(previous.get("schema_version")) is not int
            or previous["schema_version"] != 1
            or previous.get("preset") not in {"slim", "standard"}
            or not isinstance(previous.get("extras"), list)
            or any(not isinstance(e, str) for e in previous["extras"])
        ):
            raise ValueError(f"Invalid saved install options: {receipt}")
    preset = args.preset or previous.get("preset") or ("slim" if platform == "win32" else "standard")
    extras = (set(previous.get("extras", [])) | set(args.extra)) - set(args.without_extra)
    available = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"][
        "optional-dependencies"
    ]
    if any(e not in available or e in {"slim", "standard"} for e in extras):
        raise ValueError("Unknown extra (or preset used as extra); use --preset slim|standard")
    options = {"schema_version": 1, "preset": preset, "extras": sorted(extras)}
    commands = []
    if preset == "standard" or "web" in extras:
        commands.extend(
            [["npm", "--prefix", "frontend", "ci"], ["npm", "--prefix", "frontend", "run", "build"]]
        )
    sync = [
        "uv",
        "sync",
        "--locked",
        "--python",
        "3.12",
        "--extra",
        preset,
        "--no-default-groups",
        "--no-editable",
        "--inexact",
    ]
    for extra in sorted(extras):
        sync.extend(["--extra", extra])
    commands.append(sync)
    return target, options, commands


def install(root, args, *, environment=None, platform=None, run=subprocess.run):
    environment = dict(os.environ if environment is None else environment)
    target, options, commands = plan(root, args, environment, platform or sys.platform)
    environment["UV_PROJECT_ENVIRONMENT"] = str(target)
    environment.setdefault("UV_LINK_MODE", "copy")
    environment["PYTHONUTF8"] = "1"
    print(f"Environment: {target}", flush=True)
    print(f"Preset: {options['preset']}; extras: {', '.join(options['extras']) or '(none)'}", flush=True)
    for command in commands:
        print("  " + shlex.join(command), flush=True)
    if args.dry_run:
        print("Preview only; no build, installation or instance changes.")
        return
    executables = {}
    for command in commands:
        name = command[0]
        executable = shutil.which(name, path=environment.get("PATH"))
        if not executable:
            raise ValueError(f"Required executable not found: {name}; install it and reopen the terminal")
        executables[name] = executable
    for command in commands:
        run([executables[command[0]], *command[1:]], cwd=root, env=environment, check=True)
    target.mkdir(parents=True, exist_ok=True)
    # Save only after every step succeeds; failed attempts retain the previous choices.
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=target, delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(options, stream, indent=2)
        os.replace(temporary, target / "pa-install.json")
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    print("Installed. Existing instances were not initialized, started or restarted.")
    print(f"PA command: {target / ('Scripts/pa.exe' if (platform or sys.platform) == 'win32' else 'bin/pa')}")


def arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--preset", choices=("slim", "standard"), help="Saved choice, or Windows slim / Linux standard"
    )
    parser.add_argument(
        "--extra", action="append", default=[], help="Add a dependency extra; repeatable and remembered"
    )
    parser.add_argument(
        "--without-extra",
        action="append",
        default=[],
        help="Stop selecting an extra; does not uninstall packages",
    )
    parser.add_argument("--dry-run", action="store_true", help="Show the plan without building or installing")
    return parser.parse_args(argv)


def main():
    try:
        install(ROOT, arguments())
    except subprocess.CalledProcessError as exc:
        print(
            f"Installation failed (exit {exc.returncode}); saved choices were not changed.", file=sys.stderr
        )
        return exc.returncode if exc.returncode > 0 else 1
    except (ValueError, OSError) as exc:
        print(f"install: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
