#!/usr/bin/env python
"""Full verification: tests + docs + deployment static checks + examples.

Usage::

    python scripts/verify.py
    python scripts/verify.py --skip-examples
    python scripts/verify.py --keep      # keep .verify-tmp for debugging
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VERIFY_DIR = ROOT / ".verify-tmp"
EXAMPLES = sorted((ROOT / "examples").glob("[0-9][0-9]_*.py"))


def run(cmd: list[str], *, env: dict[str, str] | None = None, timeout: int = 600) -> subprocess.CompletedProcess[str]:
    child_env = dict(env if env is not None else os.environ)
    child_env.setdefault("PYTHONIOENCODING", "utf-8")
    return subprocess.run(
        cmd,
        cwd=ROOT,
        env=child_env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def tail(text: str, limit: int = 4000) -> str:
    text = text.strip()
    return text[-limit:]


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-examples", action="store_true")
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args()

    if VERIFY_DIR.exists():
        shutil.rmtree(VERIFY_DIR)
    VERIFY_DIR.mkdir(parents=True)

    print("[1/4] pytest")
    result = run([sys.executable, "-m", "pytest", "-q"])
    print(tail(result.stdout, 2500))
    if result.returncode != 0:
        print(tail(result.stderr), file=sys.stderr)
        print("FAILED: pytest", file=sys.stderr)
        return 1
    match = re.search(r"(\d+) passed", result.stdout)
    passed = match.group(1) if match else "?"

    print("\n[2/4] documentation")
    result = run([sys.executable, "scripts/check_docs.py"])
    print(result.stdout.strip())
    if result.returncode != 0:
        print(tail(result.stderr), file=sys.stderr)
        print("FAILED: documentation check", file=sys.stderr)
        return 1

    print("\n[3/4] deployment")
    result = run([sys.executable, "scripts/check_deploy.py"])
    print(result.stdout.strip())
    if result.returncode != 0:
        print(tail(result.stderr), file=sys.stderr)
        print("FAILED: deployment check", file=sys.stderr)
        return 1

    print("\n[4/4] examples")
    if args.skip_examples:
        print("skipped")
    else:
        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT)
        env["MINI_MLFLOW_TRACKING_URI"] = f"sqlite:///{(VERIFY_DIR / 'examples.db').as_posix()}"
        env["MINI_MLFLOW_ARTIFACT_ROOT"] = str(VERIFY_DIR / "artifacts")
        env["MINI_MLFLOW_DEMO_SUFFIX"] = "verify"
        env["MINI_MLFLOW_USER"] = "verify"
        for example in EXAMPLES:
            print(f"  - {example.relative_to(ROOT)}")
            result = run([sys.executable, str(example)], env=env, timeout=180)
            if result.returncode != 0:
                print(tail(result.stdout), file=sys.stderr)
                print(tail(result.stderr), file=sys.stderr)
                print(f"FAILED: {example.name}", file=sys.stderr)
                return 1

    docs_dir = ROOT / "docs"
    chapter_count = len(
        [
            path
            for path in docs_dir.glob("[0-9][0-9]-*.md")
            if path.name not in {"00-preface.md", "99-conclusion.md"}
        ]
    )
    appendix_count = len(list(docs_dir.glob("appendix-*.md")))

    print("\nVerification passed.")
    print(f"  tests: {passed} passed")
    print(f"  examples: {len(EXAMPLES)}")
    print(f"  docs: {chapter_count} chapters + {appendix_count} appendices")
    print("  deployment: static checks passed")
    if not args.keep:
        shutil.rmtree(VERIFY_DIR, ignore_errors=True)
    else:
        print(f"  kept: {VERIFY_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
