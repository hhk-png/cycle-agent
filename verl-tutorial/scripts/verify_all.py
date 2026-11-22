#!/usr/bin/env python3
"""Run the three verification commands in order."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run(args: list[str]) -> None:
    print("$", " ".join(args))
    completed = subprocess.run(args, cwd=ROOT)
    if completed.returncode != 0:
        raise SystemExit(completed.returncode)


def main() -> int:
    run([sys.executable, "-m", "pytest", "tests/", "-q"])
    run([sys.executable, "-X", "utf8", "examples/run_all.py"])
    run([sys.executable, "-X", "utf8", "check_docs.py"])
    print("all verification steps passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
