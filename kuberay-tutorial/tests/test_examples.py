"""Keep the runnable labs honest.

The `examples/` scripts are part of the tutorial's contract: every command in
the README should still work.  Running their `main()` in-process turns that
promise into a normal pytest failure instead of a broken documentation example.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parent.parent


def load_example(name: str) -> ModuleType:
    path = ROOT / "examples" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"example_{name}", path)
    assert spec and spec.loader, f"cannot load {path}"
    module = importlib.util.module_from_spec(spec)
    # Dataclasses (and pickling) look the module up in sys.modules; the
    # default importlib recipe omits this step for standalone scripts.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_rayjob_modes_example() -> None:
    assert load_example("rayjob_modes").main() == 0


def test_cronjob_lab_example() -> None:
    assert load_example("cronjob_lab").main() == 0


def test_cluster_lifecycle_example() -> None:
    assert load_example("cluster_lifecycle").main() == 0


def test_data_execution_lab() -> None:
    assert load_example("data_execution_lab").main() == 0


def test_elastic_training_lab() -> None:
    assert load_example("elastic_training_lab").main() == 0


def test_llm_scheduler_lab() -> None:
    assert load_example("llm_scheduler_lab").main() == 0


def test_observability_lab() -> None:
    assert load_example("observability_lab").main() == 0


def test_policy_lab() -> None:
    assert load_example("policy_lab").main() == 0


def test_governance_lab() -> None:
    assert load_example("governance_lab").main() == 0


def test_elasticity_lab() -> None:
    assert load_example("elasticity_lab").main() == 0


def test_lifecycle_lab() -> None:
    assert load_example("lifecycle_lab").main() == 0


def test_dev_loop_lab() -> None:
    assert load_example("dev_loop_lab").main() == 0


def test_operator_reliability_lab() -> None:
    assert load_example("operator_reliability_lab").main() == 0


def test_maintenance_lab() -> None:
    assert load_example("maintenance_lab").main() == 0


def test_diagnosis_lab() -> None:
    assert load_example("diagnosis_lab").main() == 0


def test_ippr_lab() -> None:
    assert load_example("ippr_lab").main() == 0


def test_k8s_primitives_lab() -> None:
    assert load_example("k8s_primitives_lab").main() == 0
