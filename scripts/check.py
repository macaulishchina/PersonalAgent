"""Single repeatable development gate; never touches the installed service."""

from pathlib import Path
import subprocess
import sys

root = Path(__file__).resolve().parents[1]
# An installed frontend must type-check and build before HTTP/browser tests.
if (root / "frontend/node_modules").exists():
    subprocess.run(["npm", "--prefix", "frontend", "run", "build"], cwd=root, check=True)
for args in [
    ["-m", "ruff", "format", "--check", "src", "tests", "scripts"],
    ["-m", "ruff", "check", "src", "tests", "scripts"],
    ["-m", "mypy"],
    ["-m", "unittest", "discover", "-s", "tests", "-v"],
]:
    subprocess.run([sys.executable, *args], cwd=root, check=True)
