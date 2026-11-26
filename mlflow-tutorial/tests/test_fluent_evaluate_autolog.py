from __future__ import annotations

import importlib
from pathlib import Path

import pytest

import mini_mlflow as mlflow
from mini_mlflow.client import MiniClient
from mini_mlflow.errors import InvalidParameter
from mini_mlflow.evaluate import (
    classification_metrics,
    evaluate_model,
    evaluate_predictions,
    llm_metrics,
    log_loss,
    regression_metrics,
    roc_auc_score,
)
from mini_mlflow.models import LinearRegression


def _configure_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> str:
    db = tmp_path / "fluent.db"
    artifacts = tmp_path / "fluent-artifacts"
    tracking_uri = f"sqlite:///{db.as_posix()}"
    monkeypatch.setenv("MINI_MLFLOW_TRACKING_URI", tracking_uri)
    monkeypatch.setenv("MINI_MLFLOW_ARTIFACT_ROOT", str(artifacts))
    return tracking_uri


def test_fluent_run_success(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    with mlflow.start_run(run_name="fluent") as active:
        mlflow.log_param("alpha", 0.1)
        mlflow.log_metric("score", 0.9)
        mlflow.set_tag("team", "nlp")
        run_id = active.info.run_id
        assert active.info.status == "RUNNING"
    client = MiniClient()
    run = client.get_run(run_id)
    assert run.info.status == "FINISHED"
    assert run.data.params["alpha"] == "0.1"
    assert run.data.metrics["score"] == 0.9
    client.close()


def test_fluent_exception_marks_failed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    run_id = None
    with pytest.raises(RuntimeError):
        with mlflow.start_run(run_name="boom") as active:
            run_id = active.info.run_id
            raise RuntimeError("boom")
    client = MiniClient()
    assert client.get_run(run_id).info.status == "FAILED"
    client.close()


def test_fluent_nested_run(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    with mlflow.start_run(run_name="parent") as parent:
        with mlflow.start_run(run_name="child", nested=True) as child:
            client = MiniClient()
            child_run = client.get_run(child.info.run_id)
            assert child_run.data.tags["mlflow.parentRunId"] == parent.info.run_id
            client.close()


def test_fluent_requires_active_run(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    with pytest.raises(InvalidParameter, match="no active run"):
        mlflow.log_metric("x", 1)


def test_fluent_duplicate_run_rejected(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    with mlflow.start_run():
        with pytest.raises(InvalidParameter, match="already active"):
            mlflow.start_run()


def test_fluent_log_model(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    model = LinearRegression().fit([{"x": 1.0}, {"x": 2.0}], [2.0, 4.0])
    with mlflow.start_run() as active:
        info = mlflow.log_model(model, input_example=[{"x": 3.0}])
    assert info.model_uri.startswith(f"runs:/{active.info.run_id}/")
    loaded = mlflow.load_model(info.model_uri)
    assert loaded.predict([{"x": 4.0}]) == pytest.approx([8.0])


def test_regression_metrics_exact() -> None:
    metrics = regression_metrics([1.0, 2.0, 3.0], [1.0, 2.0, 3.0])
    assert metrics["mse"] == 0
    assert metrics["r2"] == 1
    assert metrics["mae"] == 0


def test_classification_metrics() -> None:
    result = classification_metrics(["cat", "dog", "cat", "dog"], ["cat", "cat", "dog", "dog"])
    assert result["metrics"]["accuracy"] == 0.5
    assert result["confusion_matrix"]["cat"]["cat"] == 1
    assert result["confusion_matrix"]["dog"]["cat"] == 1
    assert result["labels"] == ["cat", "dog"]
    with pytest.raises(InvalidParameter):
        classification_metrics(["x"], ["y"], labels=["y"])


def test_auc_and_log_loss() -> None:
    assert roc_auc_score([0, 0, 1, 1], [0.1, 0.2, 0.8, 0.9]) == 1.0
    assert roc_auc_score([0, 1], [0.5, 0.5]) == 0.5
    assert log_loss([1, 0], [1.0, 0.0]) == pytest.approx(0.0, abs=1e-12)
    with pytest.raises(InvalidParameter):
        roc_auc_score([1, 1], [0.1, 0.2])


def test_llm_metrics_and_rouge() -> None:
    metrics = llm_metrics(["the cat sat", "完全不同的答案"], ["the cat sat", "完全不同的答案"])
    assert metrics["exact_match"] == 1.0
    assert metrics["rouge_l"] == 1.0
    assert metrics["json_validity"] == 0.0


def test_evaluate_predictions_tasks() -> None:
    assert evaluate_predictions([1, 2], [1, 3], task="regression")["metrics"]["mse"] == 0.5
    assert evaluate_predictions(["a", "b"], ["a", "b"], task="classification")["metrics"]["accuracy"] == 1.0
    assert evaluate_predictions(["x"], ["x"], task="llm")["metrics"]["exact_match"] == 1.0
    with pytest.raises(InvalidParameter):
        evaluate_predictions([1], [1], task="ranking")


def test_evaluate_model_logs_to_run(service) -> None:
    class FixedModel:
        def predict(self, data):
            return [item["x"] + 1 for item in data]

    run = service.start_run()
    result = evaluate_model(
        FixedModel(),
        [{"x": 1.0}, {"x": 2.0}],
        [2.0, 3.0],
        client=service,
        run_id=run.info.run_id,
        prefix="eval",
    )
    assert result.metrics["rmse"] == 0.0
    stored = service.get_run(run.info.run_id)
    assert stored.data.metrics["eval_rmse"] == 0.0
    assert "evaluation/report.json" in {item.path for item in service.list_artifacts(run.info.run_id, "evaluation")}


def test_evaluate_requires_active_run(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    class FixedModel:
        def predict(self, data):
            return [1.0 for _ in data]
    with pytest.raises(InvalidParameter, match="active run"):
        mlflow.evaluate(FixedModel(), [{"x": 1}], [1.0])


def test_autolog_logs_fit(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    autolog_mod = importlib.import_module("mini_mlflow.autolog")
    autolog_mod.autolog(log_input_examples=True)
    try:
        with mlflow.start_run() as active:
            model = LinearRegression(alpha=0.01).fit(
                [{"x": 1.0}, {"x": 2.0}, {"x": 3.0}],
                [2.0, 4.0, 6.0],
            )
        client = MiniClient()
        run = client.get_run(active.info.run_id)
        assert run.data.params["alpha"] == "0.01"
        assert "training_rmse" in run.data.metrics
        assert run.data.tags["estimator_name"].endswith("LinearRegression")
        assert "model" in {item.path for item in client.list_artifacts(active.info.run_id)}
        client.close()
    finally:
        autolog_mod.disable_autolog()


def test_autolog_disabled_does_not_log(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    with mlflow.start_run() as active:
        LinearRegression().fit([{"x": 1.0}, {"x": 2.0}], [2.0, 4.0])
    client = MiniClient()
    run = client.get_run(active.info.run_id)
    assert "training_rmse" not in run.data.metrics
    assert "fit_intercept" not in run.data.params
    client.close()


def test_set_tracking_uri_while_active_rejected(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    with mlflow.start_run():
        with pytest.raises(InvalidParameter, match="active"):
            mlflow.set_tracking_uri("sqlite:///other.db")


def test_confusion_matrix_rows_are_actual_labels() -> None:
    result = classification_metrics(["cat", "cat", "dog"], ["cat", "dog", "dog"])
    assert result["confusion_matrix"]["cat"]["cat"] == 1
    assert result["confusion_matrix"]["cat"]["dog"] == 0
    assert result["confusion_matrix"]["dog"]["cat"] == 1
    assert result["confusion_matrix"]["dog"]["dog"] == 1
