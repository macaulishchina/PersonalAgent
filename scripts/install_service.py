"""Install a Linux or Windows service without starting it."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from assistant_runtime.app.install_service import main, unit_text

__all__ = ["main", "unit_text"]

if __name__ == "__main__":
    main()
