"""Run the three verification layers and stop at the first failure."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
COMMANDS = [
    ("pytest", [sys.executable, "-m", "pytest"]),
    ("examples", [sys.executable, "examples/run_all.py"]),
    ("docs", [sys.executable, "check_docs.py"]),
]


def main() -> int:
    for name, command in COMMANDS:
        print(f"\n=== {name}: {' '.join(command)} ===")
        result = subprocess.run(command, cwd=ROOT)
        if result.returncode != 0:
            print(f"{name} failed with exit code {result.returncode}")
            return result.returncode
    print("\nall verification layers passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
