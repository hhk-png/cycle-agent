from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from mini_mlflow import cli
from mini_mlflow.client import MiniClient
from mini_mlflow.config import MiniConfig
from mini_mlflow.errors import InvalidParameter, ResourceAlreadyExists, ResourceNotFound
from mini_mlflow.monitoring import _chi2_sf, _ks_two_sample, _psi
from mini_mlflow.server import create_server


def _rows(*, offset: int = 0, plan: str | None = None) -> list[dict]:
    return [
        {
            "tenure": float(index + offset),
            "plan": plan or ("basic" if index % 2 else "pro"),
            "calls": float(index % 5),
        }
        for index in range(1, 101)
    ]


def _features() -> dict[str, str]:
    return {"tenure": "numeric", "plan": "categorical", "calls": "numeric"}


def test_create_monitor_baseline_and_dataset_link(service) -> None:
    dataset = service.datasets.create_dataset(
        "churn-baseline",
        [{"inputs": row, "tags": {"slice": "baseline"}} for row in _rows()],
        source="s3://warehouse/churn/2026-11-01.parquet",
        source_type="s3",
    )
    monitor = service.monitoring.create_monitor(
        "churn-monitor",
        [],
        _features(),
        model_name="churn",
        model_version="3",
        dataset_id=dataset.dataset_id,
        tags={"owner": "risk"},
    )
    assert monitor.baseline["record_count"] == 100
    assert monitor.baseline["digest"].startswith("sha256:")
    assert monitor.baseline["features"]["tenure"]["kind"] == "numeric"
    assert monitor.baseline["features"]["plan"]["counts"]["basic"] == 50
    assert monitor.baseline_dataset_id == dataset.dataset_id
    assert monitor.baseline_dataset_digest == dataset.digest
    assert monitor.tags == {"owner": "risk"}

    fetched = service.monitoring.get_monitor(monitor.monitor_id)
    assert fetched.name == "churn-monitor"
    assert [item.monitor_id for item in service.monitoring.list_monitors()] == [
        monitor.monitor_id
    ]

    with pytest.raises(ResourceAlreadyExists):
        service.monitoring.create_monitor("churn-monitor", _rows(), _features())


def test_stable_window_is_stable_and_persisted(service) -> None:
    monitor = service.monitoring.create_monitor("stable", _rows(), _features())
    window = service.monitoring.record_window(
        monitor.monitor_id, _rows(), window_start=1000, window_end=2000
    )
    assert window.status == "STABLE"
    assert window.sample_count == 100
    assert window.metrics["tenure"]["psi"] < 0.01
    assert window.report["baseline_digest"] == monitor.baseline["digest"]
    assert window.report["thresholds"]["psi_alert"] == 0.25

    listed = service.monitoring.list_windows(monitor.monitor_id)
    assert [item.window_id for item in listed] == [window.window_id]


def test_numeric_drift_alerts(service) -> None:
    monitor = service.monitoring.create_monitor("numeric", _rows(), _features())
    window = service.monitoring.record_window(
        monitor.monitor_id, _rows(offset=1000)
    )
    assert window.status == "ALERT"
    tenure = window.metrics["tenure"]
    assert tenure["psi"] >= 0.25
    assert tenure["ks_stat"] >= 0.1
    assert tenure["ks_pvalue"] <= 0.01
    assert any("PSI" in reason for reason in tenure["reasons"])


def test_categorical_unseen_category_alerts(service) -> None:
    monitor = service.monitoring.create_monitor("categorical", _rows(), _features())
    window = service.monitoring.record_window(
        monitor.monitor_id, _rows(plan="enterprise")
    )
    assert window.status == "ALERT"
    plan = window.metrics["plan"]
    assert plan["unseen_rate"] == 1.0
    assert plan["unseen_categories"] == ["enterprise"]
    assert plan["cramers_v"] == 1.0


def test_missing_and_empty_features(service) -> None:
    monitor = service.monitoring.create_monitor("missing", _rows(), _features())
    window = service.monitoring.record_window(
        monitor.monitor_id, [{"plan": "pro", "calls": 1} for _ in range(50)]
    )
    assert window.status == "ALERT"
    tenure = window.metrics["tenure"]
    assert tenure["missing_rate"] == 1.0
    assert tenure["status"] == "ALERT"
    assert tenure["reasons"] == ["no non-missing values in window"]


def test_thresholds_and_small_sample_warn(service) -> None:
    monitor = service.monitoring.create_monitor(
        "quiet",
        _rows(),
        _features(),
        thresholds={"psi_warn": 0.5, "psi_alert": 0.9},
    )
    # A moderate shift crosses the default PSI alert but not this monitor's.
    window = service.monitoring.record_window(
        monitor.monitor_id, _rows(offset=8), window_start=1, window_end=2
    )
    assert window.status in {"STABLE", "WARN"}

    tiny = service.monitoring.record_window(
        monitor.monitor_id, _rows()[:2], window_start=3, window_end=4
    )
    assert tiny.status in {"WARN", "ALERT"}
    assert any("min_samples_warn" in reason for reason in tiny.report["reasons"])


def test_constant_numeric_baseline(service) -> None:
    rows = [{"score": 5, "plan": "pro"} for _ in range(40)]
    monitor = service.monitoring.create_monitor(
        "constant", rows, {"score": "numeric", "plan": "categorical"}
    )
    stable = service.monitoring.record_window(monitor.monitor_id, rows)
    assert stable.status == "STABLE"
    shifted = service.monitoring.record_window(
        monitor.monitor_id, [{"score": 6, "plan": "pro"} for _ in range(40)]
    )
    assert shifted.status == "ALERT"
    assert shifted.metrics["score"]["constant"] == 5.0


def test_delete_monitor_cascades_windows(service) -> None:
    monitor = service.monitoring.create_monitor("cascade", _rows(), _features())
    service.monitoring.record_window(monitor.monitor_id, _rows())
    service.monitoring.delete_monitor(monitor.monitor_id)
    with pytest.raises(ResourceNotFound):
        service.monitoring.get_monitor(monitor.monitor_id)
    with pytest.raises(ResourceNotFound):
        service.monitoring.list_windows(monitor.monitor_id)


def test_invalid_inputs(service) -> None:
    with pytest.raises(InvalidParameter):
        service.monitoring.create_monitor("empty", [], _features())
    with pytest.raises(InvalidParameter):
        service.monitoring.create_monitor("bad-kind", _rows(), {"tenure": "text"})
    with pytest.raises(InvalidParameter):
        service.monitoring.create_monitor("bad-threshold", _rows(), _features(), thresholds={"psi": 1})
    with pytest.raises(InvalidParameter):
        service.monitoring.create_monitor(
            "nan", [{"tenure": float("nan"), "plan": "pro", "calls": 1}], _features()
        )
    monitor = service.monitoring.create_monitor("ok", _rows(), _features())
    with pytest.raises(InvalidParameter):
        service.monitoring.record_window(monitor.monitor_id, [])
    with pytest.raises(InvalidParameter):
        service.monitoring.record_window(
            monitor.monitor_id, _rows(), window_start=10, window_end=1
        )
    dataset = service.datasets.create_dataset(
        "both", [{"inputs": row} for row in _rows()]
    )
    with pytest.raises(InvalidParameter):
        service.monitoring.create_monitor(
            "both", _rows(), _features(), dataset_id=dataset.dataset_id
        )


def test_statistics_helpers() -> None:
    assert _psi([10, 10, 10], [10, 10, 10]) < 1e-9
    assert _psi([10, 10, 10], [0, 0, 30]) > 1.0
    assert _ks_two_sample([1, 2, 3], [1, 2, 3]) == (0.0, 1.0)
    distance, pvalue = _ks_two_sample(list(range(100)), list(range(50, 150)))
    assert distance == 0.5
    assert pvalue < 1e-6
    # Wilson-Hilferty check against the classic 0.05 critical values.
    assert abs(_chi2_sf(3.841, 1) - 0.05) < 0.01
    assert abs(_chi2_sf(5.991, 2) - 0.05) < 0.01
    assert abs(_chi2_sf(7.815, 3) - 0.05) < 0.02


@pytest.fixture()
def running_server(config: MiniConfig):
    server_config = config.derive(auth_token="secret-token")
    server = create_server(server_config, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    try:
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_remote_monitor_roundtrip(running_server: str) -> None:
    client = MiniClient(running_server, auth_token="secret-token")
    try:
        monitor = client.create_monitor(
            "remote-monitor",
            _rows(),
            _features(),
            model_name="churn",
            thresholds={"psi_alert": 0.3},
            tags={"owner": "risk"},
        )
        assert monitor.baseline["record_count"] == 100
        fetched = client.get_monitor(monitor.monitor_id)
        assert fetched.thresholds["psi_alert"] == 0.3
        assert [item.monitor_id for item in client.list_monitors()] == [
            monitor.monitor_id
        ]

        window = client.record_monitor_window(
            monitor.monitor_id, _rows(offset=1000), window_start=10, window_end=20
        )
        assert window.status == "ALERT"
        assert window.window_start == 10
        assert [item.window_id for item in client.list_monitor_windows(monitor.monitor_id)] == [
            window.window_id
        ]

        client.delete_monitor(monitor.monitor_id)
        with pytest.raises(ResourceNotFound):
            client.get_monitor(monitor.monitor_id)
    finally:
        client.close()


def test_cli_monitor_create_and_window(tmp_path: Path, capsys) -> None:
    baseline_path = tmp_path / "baseline.json"
    window_path = tmp_path / "window.json"
    baseline_path.write_text(json.dumps(_rows()), encoding="utf-8")
    window_path.write_text(json.dumps(_rows(offset=1000)), encoding="utf-8")
    uri = f"sqlite:///{(tmp_path / 'cli.db').as_posix()}"

    code = cli.main(
        [
            "monitoring",
            "create",
            "cli-monitor",
            "--baseline-file",
            str(baseline_path),
            "--feature",
            "tenure:numeric",
            "--feature",
            "plan:categorical",
            "--model-name",
            "churn",
            "--threshold",
            "psi_alert=0.3",
            "--tracking-uri",
            uri,
        ]
    )
    assert code == 0
    created = json.loads(capsys.readouterr().out)
    monitor_id = created["monitor_id"]
    assert created["thresholds"]["psi_alert"] == 0.3

    code = cli.main(
        [
            "monitoring",
            "window",
            monitor_id,
            "--records-file",
            str(window_path),
            "--window-start",
            "100",
            "--window-end",
            "200",
            "--tracking-uri",
            uri,
        ]
    )
    assert code == 0
    window = json.loads(capsys.readouterr().out)
    assert window["status"] == "ALERT"
    assert window["sample_count"] == 100

    code = cli.main(["monitoring", "windows", monitor_id, "--tracking-uri", uri])
    assert code == 0
    listed = json.loads(capsys.readouterr().out)
    assert [item["window_id"] for item in listed["windows"]] == [window["window_id"]]

    code = cli.main(["monitoring", "delete", monitor_id, "--tracking-uri", uri])
    assert code == 0
    assert json.loads(capsys.readouterr().out)["status"] == "deleted"
