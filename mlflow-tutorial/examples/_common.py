"""Shared helpers for runnable examples.

Examples use a local SQLite database under ``examples/runtime`` by default, or
the tracking server in ``MINI_MLFLOW_TRACKING_URI`` when set.  They are safe to
rerun: experiment names get a timestamp suffix unless an existing one is
explicitly reused.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

# Allow `python examples/01_quickstart.py` without installing the package.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import mini_mlflow as mlflow
from mini_mlflow.client import MiniClient
from mini_mlflow.config import MiniConfig

RUNTIME = Path(__file__).resolve().parent / "runtime"


def local_config(name: str = "mlflow.db") -> MiniConfig:
    return MiniConfig(
        backend_store_uri=f"sqlite:///{(RUNTIME / name).as_posix()}",
        artifact_root=str(RUNTIME / "artifacts"),
        port=0,
    )


def client() -> MiniClient:
    uri = os.environ.get("MINI_MLFLOW_TRACKING_URI")
    if uri:
        return MiniClient(uri)
    RUNTIME.mkdir(parents=True, exist_ok=True)
    return MiniClient(config=local_config())


def unique_experiment(client: MiniClient, base: str) -> Any:
    suffix = os.environ.get("MINI_MLFLOW_DEMO_SUFFIX") or str(int(time.time() * 1000))[-6:]
    name = f"{base}-{suffix}"
    experiment = client.get_experiment_by_name(name)
    if experiment is None:
        experiment = client.create_experiment(name, tags={"demo": "true"})
    return experiment


def print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def configure_fluent(active_client: MiniClient) -> None:
    """Point the fluent API at the same backend as an explicit client."""
    config = active_client.local_config
    if config is not None:
        os.environ["MINI_MLFLOW_ARTIFACT_ROOT"] = str(config.artifact_root_path)
        os.environ["MINI_MLFLOW_BACKEND_STORE_URI"] = config.tracking_uri
    mlflow.set_tracking_uri(active_client.tracking_uri)
