"""Run every numbered example in a fresh subprocess and verify its exit code."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")


ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = sorted(
    path
    for path in ROOT.glob("examples/[0-9][0-9]_*.py")
    if path.is_file()
)


def main() -> int:
    if not EXAMPLES:
        print("no examples found", file=sys.stderr)
        return 1
    failures = 0
    for example in EXAMPLES:
        print(f"==> {example.name}")
        process = subprocess.run(
            [sys.executable, str(example)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
        if process.stdout:
            print(process.stdout.rstrip())
        if process.returncode != 0:
            failures += 1
            print(f"FAILED: {example.name} (exit {process.returncode})", file=sys.stderr)
            if process.stderr:
                print(process.stderr.rstrip(), file=sys.stderr)
    if failures:
        print(f"EXAMPLES FAILED: {failures}/{len(EXAMPLES)}", file=sys.stderr)
        return 1
    print(f"EXAMPLES OK {len(EXAMPLES)}/{len(EXAMPLES)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
