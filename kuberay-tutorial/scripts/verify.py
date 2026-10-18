#!/usr/bin/env python3
"""Run the complete verification matrix.

    python scripts/verify.py

This is a convenience wrapper; the individual commands in README.md are the
source of truth and can be run independently.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def run(label: str, command: list[str]) -> bool:
    print(f"\n== {label} ==")
    result = subprocess.run(command, cwd=ROOT)
    if result.returncode != 0:
        print(f"FAILED: {' '.join(command)}")
        return False
    return True


def main() -> int:
    checks = [
        ("tests", [sys.executable, "-m", "pytest", "-q"]),
        ("documentation", [sys.executable, "check_docs.py"]),
        ("demo", [sys.executable, "-m", "minikuberay", "demo", "--fast"]),
        ("autoscaling smoke", [sys.executable, "examples/autoscaling_smoke.py"]),
        ("autoscaler walkthrough", [sys.executable, "examples/autoscaler_walkthrough.py"]),
        ("RayJob submission modes", [sys.executable, "examples/rayjob_modes.py"]),
        ("RayCluster lifecycle", [sys.executable, "examples/cluster_lifecycle.py"]),
        ("RayCronJob lab", [sys.executable, "examples/cronjob_lab.py"]),
        ("incident lab", [sys.executable, "examples/incident_lab.py"]),
        ("Ray Data execution lab", [sys.executable, "examples/data_execution_lab.py"]),
        ("elastic training lab", [sys.executable, "examples/elastic_training_lab.py"]),
        ("LLM scheduler lab", [sys.executable, "examples/llm_scheduler_lab.py"]),
        ("observability lab", [sys.executable, "examples/observability_lab.py"]),
        ("policy lab", [sys.executable, "examples/policy_lab.py"]),
        ("governance lab", [sys.executable, "examples/governance_lab.py"]),
        ("elasticity lab", [sys.executable, "examples/elasticity_lab.py"]),
        ("lifecycle lab", [sys.executable, "examples/lifecycle_lab.py"]),
        ("dev loop lab", [sys.executable, "examples/dev_loop_lab.py"]),
        ("operator reliability lab", [sys.executable, "examples/operator_reliability_lab.py"]),
        ("change management lab", [sys.executable, "examples/maintenance_lab.py"]),
        ("live diagnosis lab", [sys.executable, "examples/diagnosis_lab.py"]),
        ("IPPR lab", [sys.executable, "examples/ippr_lab.py"]),
        ("Kubernetes primitives lab", [sys.executable, "examples/k8s_primitives_lab.py"]),
        ("scale benchmark", [sys.executable, "examples/scale_benchmark.py", "--clusters", "30", "--workers", "2"]),
    ]
    for label, command in checks:
        if not run(label, command):
            return 1
    print("\nAll verification checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
