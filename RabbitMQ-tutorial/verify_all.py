#!/usr/bin/env python3
"""One-command verification: tests, examples and documentation."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")


ROOT = Path(__file__).resolve().parent
EXPECTED_TESTS = 219
EXPECTED_EXAMPLES = 22


def run(label: str, command: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
    print(f"\n== {label} ==")
    result = subprocess.run(
        command,
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    output = (result.stdout or "") + (result.stderr or "")
    print(output.rstrip())
    if result.returncode != 0:
        raise SystemExit(f"{label} failed with exit code {result.returncode}")
    return result


def main() -> int:
    tests = run(
        "pytest", [sys.executable, "-m", "pytest", "-q"], timeout=180
    )
    match = re.search(r"(\d+) passed", tests.stdout)
    if match is None or int(match.group(1)) != EXPECTED_TESTS:
        raise SystemExit(
            f"expected {EXPECTED_TESTS} passing tests, got {match.group(1) if match else 'unknown'}"
        )

    examples = run(
        "examples", [sys.executable, "examples/run_all.py"], timeout=300
    )
    match = re.search(r"EXAMPLES OK (\d+)/(\d+)", examples.stdout)
    if match is None or int(match.group(1)) != EXPECTED_EXAMPLES:
        raise SystemExit("example verification did not report the expected count")

    run("docs", [sys.executable, "check_docs.py"], timeout=120)
    print("\nVERIFY OK: 219 tests, 22 examples, 32 chapters, 26 appendices.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
