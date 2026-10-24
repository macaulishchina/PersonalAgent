"""Install the pinned Pi runtime on Linux or Windows."""

import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from assistant_runtime.app.install_pi import setup


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", nargs="?")
    args = parser.parse_args()
    print(setup(args.destination))


if __name__ == "__main__":
    main()
