"""Run the full verification suite: tests, examples, and documentation checks."""

from __future__ import annotations

import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYTHON = sys.executable


def run(label: str, command: list[str], *, capture: bool = False) -> tuple[bool, str, float]:
    started = time.time()
    print(f"\n== {label} ==")
    print("$ " + " ".join(command), flush=True)
    result = subprocess.run(command, cwd=ROOT, text=True,
                            capture_output=capture)
    elapsed = time.time() - started
    if capture:
        output = (result.stdout or "") + (result.stderr or "")
        print(output.rstrip())
    else:
        output = ""
    if result.returncode == 0:
        print(f"PASS {label} ({elapsed:.1f}s)")
    else:
        print(f"FAIL {label} ({elapsed:.1f}s), exit code {result.returncode}")
    return result.returncode == 0, output, elapsed


def collect_test_count() -> int:
    result = subprocess.run(
        [PYTHON, "-m", "pytest", "-o", "addopts=", "--collect-only", "-q"],
        cwd=ROOT, capture_output=True, text=True)
    match = re.search(r"(\d+) tests collected", result.stdout + result.stderr)
    return int(match.group(1)) if match else -1


def main() -> int:
    print("MiniK8s verification suite")
    print(f"root: {ROOT}")
    total_started = time.time()
    results: list[tuple[str, bool, float]] = []

    test_count = collect_test_count()
    print(f"collected tests: {test_count if test_count >= 0 else 'unknown'}")

    ok, output, elapsed = run("pytest", [PYTHON, "-m", "pytest", "-o", "addopts=", "-q"])
    results.append(("pytest", ok, elapsed))

    ok, output, elapsed = run("examples", [PYTHON, "examples/run_all.py"], capture=True)
    results.append(("examples", ok, elapsed))

    ok, output, elapsed = run("docs", [PYTHON, "check_docs.py"], capture=True)
    results.append(("docs", ok, elapsed))

    print("\n== summary ==")
    for label, ok, elapsed in results:
        print(f"{'PASS' if ok else 'FAIL'}  {label:9} {elapsed:6.1f}s")
    print(f"total: {time.time() - total_started:.1f}s")
    if test_count >= 0:
        print(f"tests collected: {test_count}")
    return 0 if all(ok for _, ok, _ in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
