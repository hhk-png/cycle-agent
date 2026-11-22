"""Run every numbered example and fail on the first non-zero exit."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def main() -> int:
    scripts = sorted(path for path in ROOT.glob("[0-9][0-9]_*.py"))
    for script in scripts:
        print(f"\n### {script.name}")
        completed = subprocess.run([sys.executable, "-X", "utf8", str(script)], cwd=str(ROOT))
        if completed.returncode != 0:
            print(f"FAILED: {script.name}")
            return completed.returncode
    print(f"\nall {len(scripts)} examples passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
