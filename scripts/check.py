"""Single repeatable development gate; never touches the installed service."""

from pathlib import Path
import subprocess
import sys

root = Path(__file__).resolve().parents[1]
for args in [
    ["-m", "ruff", "format", "--check", "src", "tests", "scripts"],
    ["-m", "ruff", "check", "src", "tests", "scripts"],
    ["-m", "mypy"],
    ["-m", "unittest", "discover", "-s", "tests", "-v"],
]:
    subprocess.run([sys.executable, *args], cwd=root, check=True)
