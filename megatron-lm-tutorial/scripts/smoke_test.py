#!/usr/bin/env python
"""One-command sanity check for the mini implementation."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run(argv):
    print("+", " ".join(argv), flush=True)
    result = subprocess.run(argv, cwd=ROOT, text=True, capture_output=True)
    if result.returncode != 0:
        print(result.stdout)
        print(result.stderr, file=sys.stderr)
        raise SystemExit(result.returncode)
    print(result.stdout.strip())
    return result.stdout


def main() -> int:
    py = sys.executable
    run([py, "-m", "pytest", "-q", "tests/unit"])
    run([py, "scripts/run_distributed.py", "--world-size", "2", "--config", "configs/tiny_tp2.json", "--steps", "2", "--checkpoint-dir", ""])
    run([py, "scripts/run_distributed.py", "--world-size", "2", "--config", "configs/tiny_pp2.json", "--steps", "2", "--checkpoint-dir", ""])
    run([py, "scripts/run_distributed.py", "--world-size", "2", "--config", "configs/tiny_cp2.json", "--steps", "2", "--checkpoint-dir", ""])
    run([py, "-m", "mini_megatron.cli", "train", "--config", "configs/tiny.json", "--steps", "2", "--checkpoint-dir", "checkpoints/smoke"])
    print(json.dumps({"status": "ok"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
