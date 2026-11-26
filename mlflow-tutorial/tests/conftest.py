from __future__ import annotations

from pathlib import Path

import pytest

import importlib

from mini_mlflow import tracing
from mini_mlflow.client import MiniClient
from mini_mlflow.config import MiniConfig
from mini_mlflow.tracking import TrackingService


@pytest.fixture()
def config(tmp_path: Path) -> MiniConfig:
    database = tmp_path / "mlflow.db"
    return MiniConfig(
        backend_store_uri=f"sqlite:///{database.as_posix()}",
        artifact_root=str(tmp_path / "artifacts"),
        port=0,
        search_max_results=1000,
        trace_max_payload_bytes=64 * 1024,
    )


@pytest.fixture()
def service(config: MiniConfig):
    instance = TrackingService(config)
    try:
        yield instance
    finally:
        instance.close()


@pytest.fixture()
def client(config: MiniConfig):
    instance = MiniClient(config=config)
    try:
        yield instance
    finally:
        instance.close()


@pytest.fixture(autouse=True)
def reset_global_state(monkeypatch: pytest.MonkeyPatch):
    from mini_mlflow import fluent

    tracing.reset_tracing()
    autolog = importlib.import_module("mini_mlflow.autolog")
    autolog.disable_autolog()
    monkeypatch.setattr(fluent, "_DEFAULT_CLIENT", None, raising=False)
    monkeypatch.setattr(fluent, "_TRACKING_URI_OVERRIDE", None, raising=False)
    for name in [
        "MINI_MLFLOW_TRACKING_URI",
        "MINI_MLFLOW_BACKEND_STORE_URI",
        "MINI_MLFLOW_ARTIFACT_ROOT",
        "MINI_MLFLOW_RUN_ID",
        "MINI_MLFLOW_EXPERIMENT_ID",
        "MINI_MLFLOW_AUTH_TOKEN",
        "MINI_MLFLOW_TRACKING_TOKEN",
    ]:
        monkeypatch.delenv(name, raising=False)
    yield
    tracing.reset_tracing()
    autolog = importlib.import_module("mini_mlflow.autolog")
    autolog.disable_autolog()


def make_run(service: TrackingService, *, name: str = "run", experiment_id: str = "0"):
    return service.start_run(experiment_id=experiment_id, run_name=name)
