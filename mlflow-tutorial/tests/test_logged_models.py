from __future__ import annotations

import threading
from pathlib import Path

import pytest

import mini_mlflow as mlflow
from mini_mlflow.client import MiniClient
from mini_mlflow.config import MiniConfig
from mini_mlflow.entities import LoggedModel, LoggedModelStatus, Metric
from mini_mlflow.errors import (
    InvalidParameter,
    ModelLoadError,
    ResourceNotFound,
    SearchSyntaxError,
)
from mini_mlflow.models import LinearRegression
from mini_mlflow.server import create_server
from mini_mlflow.tracking import TrackingService

MODEL_ID_RE = r"^m-[0-9a-f]{32}$"


def _run(service: TrackingService, *, name: str = "run", experiment_id: str = "0"):
    return service.start_run(experiment_id=experiment_id, run_name=name)


def test_create_logged_model_defaults_and_artifact_location(service, config) -> None:
    run = _run(service)
    model = service.create_logged_model(
        "fraud-detector",
        experiment_id="0",
        model_type="Classifier",
        source_run_id=run.info.run_id,
        tags={"team": "risk"},
        params={"alpha": 0.1, "max_depth": 6},
    )
    assert model.model_id.startswith("m-")
    assert model.model_uri == f"models:/{model.model_id}"
    assert model.status == LoggedModelStatus.PENDING.value
    assert model.tags == {"team": "risk"}
    assert model.params == {"alpha": "0.1", "max_depth": "6"}
    location = model.artifact_location
    assert location.startswith("file:")
    from mini_mlflow.utils import file_uri_to_path

    path = file_uri_to_path(location)
    assert path.is_dir()
    assert path.is_relative_to(config.artifact_root_path)

    fetched = service.get_logged_model(model.model_id)
    assert fetched.to_dict() == model.to_dict()


def test_create_logged_model_rejects_bad_links(service) -> None:
    other = service.create_experiment("other")
    run = _run(service)
    with pytest.raises(InvalidParameter):
        service.create_logged_model("x", experiment_id="0", source_run_id=run.info.run_id, model_type="x" * 200)
    with pytest.raises(InvalidParameter):
        service.create_logged_model("x", experiment_id=other.experiment_id, source_run_id=run.info.run_id)
    service.delete_experiment(other.experiment_id)
    with pytest.raises(InvalidParameter):
        service.create_logged_model("x", experiment_id=other.experiment_id)
    with pytest.raises(ResourceNotFound):
        service.create_logged_model("x", experiment_id="0", source_run_id="f" * 32)
    with pytest.raises(InvalidParameter):
        service.get_logged_model("not-a-model-id")


def test_params_are_immutable_and_tags_are_mutable(service) -> None:
    model = service.create_logged_model("m1", experiment_id="0", params={"alpha": 0.1})
    service.log_logged_model_params(model.model_id, {"alpha": 0.1, "beta": 2})
    assert service.get_logged_model(model.model_id).params == {"alpha": "0.1", "beta": "2"}
    with pytest.raises(InvalidParameter):
        service.log_logged_model_params(model.model_id, {"alpha": 0.2})

    service.set_logged_model_tags(model.model_id, {"stage": "candidate"})
    service.set_logged_model_tags(model.model_id, {"stage": "production"})
    assert service.get_logged_model(model.model_id).tags == {"stage": "production"}
    service.delete_logged_model_tag(model.model_id, "stage")
    assert service.get_logged_model(model.model_id).tags == {}
    with pytest.raises(ResourceNotFound):
        service.delete_logged_model_tag(model.model_id, "stage")


def test_metric_history_latest_value_uses_step_then_timestamp(service) -> None:
    model = service.create_logged_model("metrics", experiment_id="0")
    service.log_logged_model_metrics(model.model_id, {"acc": 0.5}, step=1, timestamp=100)
    service.log_logged_model_metrics(model.model_id, {"acc": 0.2}, step=1, timestamp=300)
    service.log_logged_model_metrics(model.model_id, {"acc": 0.7}, step=2, timestamp=50)
    service.log_logged_model_metrics(model.model_id, {"acc": 0.9}, step=2, timestamp=200)
    latest = service.get_logged_model(model.model_id).metrics["acc"]
    # A timestamp-only implementation would pick 0.2 (ts=300).  Step wins.
    assert latest.value == pytest.approx(0.9)
    assert latest.step == 2
    assert latest.timestamp == 200

    found, _ = service.search_logged_models(
        ["0"], filter_string="metrics.acc > 0.8"
    )
    assert [item.model_id for item in found] == [model.model_id]
    found, _ = service.search_logged_models(
        ["0"], filter_string="metrics.acc < 0.6"
    )
    assert found == []
    with pytest.raises(SearchSyntaxError):
        service.search_logged_models(["0"], filter_string="metrics.acc = 'high'")
    with pytest.raises(SearchSyntaxError):
        service.search_logged_models(["0"], filter_string="metrics.acc = 1e400")
    with pytest.raises(SearchSyntaxError):
        service.search_logged_models(["0"], filter_string="params.lr = 0.1")
    with pytest.raises(SearchSyntaxError):
        service.search_logged_models(
            ["0"], filter_string="creation_time = 99999999999999999999999999"
        )
    with pytest.raises(InvalidParameter):
        service.log_logged_model_metrics(model.model_id, {"acc": 1.0}, step=2**63)


def test_finalize_state_machine(service) -> None:
    model = service.create_logged_model("state", experiment_id="0")
    ready = service.finalize_logged_model(model.model_id, "READY")
    assert ready.status == LoggedModelStatus.READY.value
    again = service.finalize_logged_model(model.model_id, "LOGGED_MODEL_READY")
    assert again.status == LoggedModelStatus.READY.value
    with pytest.raises(InvalidParameter):
        service.finalize_logged_model(model.model_id, "FAILED")
    with pytest.raises(InvalidParameter):
        service.finalize_logged_model(model.model_id, "NOT_A_STATUS")


def test_search_attributes_params_and_ordering(service) -> None:
    first = service.create_logged_model(
        "agent-a", experiment_id="0", model_type="Agent", params={"temperature": 0.2}, tags={"team": "a"}
    )
    second = service.create_logged_model(
        "agent-b", experiment_id="0", model_type="LLM", params={"temperature": 0.9}, tags={"team": "b"}
    )
    service.finalize_logged_model(second.model_id, "READY")
    service.log_logged_model_metrics(second.model_id, {"quality": 0.8})

    found, token = service.search_logged_models(
        ["0"],
        filter_string="name = 'agent-a'",
        order_by=["name ASC"],
    )
    assert [item.name for item in found] == ["agent-a"]
    assert token is None

    found, _ = service.search_logged_models(
        ["0"], filter_string="status = 'READY'"
    )
    assert [item.model_id for item in found] == [second.model_id]

    found, _ = service.search_logged_models(
        ["0"], filter_string="params.temperature = '0.9' AND metrics.quality >= 0.8"
    )
    assert [item.model_id for item in found] == [second.model_id]

    ordered, _ = service.search_logged_models(
        ["0"], order_by=["metrics.quality DESC", "name ASC"]
    )
    assert [item.model_id for item in ordered] == [second.model_id, first.model_id]

    with pytest.raises(SearchSyntaxError):
        service.search_logged_models(
            ["0"], filter_string="name = 'agent-a' OR name = 'agent-b'"
        )
    with pytest.raises(SearchSyntaxError):
        service.search_logged_models(["0"], filter_string="tags.team = 'a'")
    with pytest.raises(SearchSyntaxError):
        service.search_logged_models(["0"], filter_string="name LIKE 'agent%'")
    with pytest.raises(SearchSyntaxError):
        service.search_logged_models(["0"], filter_string="NOT name = 'agent-a'")


def test_search_pagination_and_experiment_scope(service) -> None:
    for index in range(5):
        service.create_logged_model(f"m-{index}", experiment_id="0")
    other = service.create_experiment("other")
    service.create_logged_model("other-model", experiment_id=other.experiment_id)

    page1, token = service.search_logged_models(
        ["0"], order_by=["name ASC"], max_results=2
    )
    assert [item.name for item in page1] == ["m-0", "m-1"]
    assert token
    page2, token = service.search_logged_models(
        ["0"], order_by=["name ASC"], max_results=2, page_token=token
    )
    assert [item.name for item in page2] == ["m-2", "m-3"]
    page3, token = service.search_logged_models(
        ["0"], order_by=["name ASC"], max_results=2, page_token=token
    )
    assert [item.name for item in page3] == ["m-4"]
    assert token is None

    for _ in range(3):
        service.create_logged_model("same-name", experiment_id="0")
    tie_page, tie_token = service.search_logged_models(
        ["0"], filter_string="name = 'same-name'", order_by=["name ASC"], max_results=2
    )
    assert len(tie_page) == 2 and tie_token
    assert len({item.model_id for item in tie_page}) == 2

    scoped, _ = service.search_logged_models(
        [other.experiment_id], order_by=["name ASC"]
    )
    assert [item.name for item in scoped] == ["other-model"]
    with pytest.raises(InvalidParameter):
        service.search_logged_models([str(index) for index in range(11)])


def test_delete_and_experiment_cascade(service) -> None:
    model = service.create_logged_model("delete-me", experiment_id="0")
    service.delete_logged_model(model.model_id)
    with pytest.raises(ResourceNotFound):
        service.get_logged_model(model.model_id)

    experiment = service.create_experiment("cascade")
    kept = service.create_logged_model("kept", experiment_id=experiment.experiment_id)
    service.delete_experiment(experiment.experiment_id)
    with pytest.raises(ResourceNotFound):
        service.get_logged_model(kept.model_id)
    # Mutations must fail *before* committing, not commit then report 404.
    with pytest.raises(ResourceNotFound):
        service.finalize_logged_model(kept.model_id, "READY")
    with pytest.raises(ResourceNotFound):
        service.set_logged_model_tags(kept.model_id, {"hidden": "yes"})
    with pytest.raises(ResourceNotFound):
        service.log_logged_model_params(kept.model_id, {"b": "2"})
    with pytest.raises(ResourceNotFound):
        service.delete_logged_model(kept.model_id)
    service.restore_experiment(experiment.experiment_id)
    restored = service.get_logged_model(kept.model_id)
    assert restored.status == LoggedModelStatus.PENDING.value
    assert restored.tags == {} and restored.params == {}


def test_log_model_with_name_and_load_by_model_uri(client) -> None:
    run = client.start_run(experiment_id="0", run_name="training")
    x = [[1.0, 0.0], [0.0, 1.0], [1.0, 1.0], [2.0, 1.0], [1.0, 2.0]]
    y = [3.0, 5.0, 7.0, 9.0, 11.0]
    model = LinearRegression(alpha=0.1).fit(x, y)
    info = client.log_model(
        run.info.run_id,
        model,
        name="linear-agent",
        model_type="Regressor",
        params={"alpha": 0.1},
        input_example=x[:1],
    )
    assert info.model_id is not None
    assert info.model_uri == f"models:/{info.model_id}"
    assert info.model_type == "Regressor"
    logged = client.get_logged_model(info.model_id)
    assert logged.status == LoggedModelStatus.READY.value
    assert logged.params == {"alpha": "0.1"}
    from mini_mlflow.utils import file_uri_to_path

    assert (file_uri_to_path(logged.artifact_location) / "MLmodel").exists()

    expected = model.predict([[3.0, 3.0]])
    loaded = client.load_model(info.model_uri)
    assert loaded.predict([[3.0, 3.0]]) == pytest.approx(expected, rel=1e-6)

    # Registering the logged model keeps the model-centric source.
    info2 = client.log_model(
        run.info.run_id,
        model,
        name="registered",
        registered_model_name="agent",
    )
    version = client.get_model_version("agent", info2.version)
    assert version.source == f"models:/{info2.model_id}"
    loaded_from_registry = client.load_model(f"models:/agent/{info2.version}")
    assert loaded_from_registry.predict([[3.0, 3.0]]) == pytest.approx(expected, rel=1e-6)

    empty = client.create_logged_model("not-uploaded", experiment_id="0")
    with pytest.raises(ModelLoadError):
        client.load_model(empty.model_uri)


def test_remote_logged_model_roundtrip(config: MiniConfig) -> None:
    server_config = config.derive(auth_token="secret-token")
    server = create_server(server_config, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    remote = MiniClient(f"http://{host}:{port}", auth_token="secret-token")
    try:
        model = remote.create_logged_model(
            "remote-model",
            experiment_id="0",
            model_type="LLM",
            tags={"team": "nlp"},
            params={"temperature": 0.2},
        )
        assert model.status == LoggedModelStatus.PENDING.value
        remote.finalize_logged_model(model.model_id, "READY")
        remote.set_logged_model_tags(
            model.model_id,
            {"stage": "candidate", "owner/team": "nlp", "pct%2Fkey": "v"},
        )
        remote.log_logged_model_params(model.model_id, {"max_tokens": 128})
        remote.log_logged_model_metrics(model.model_id, {"latency": 0.4}, step=2)
        fetched = remote.get_logged_model(model.model_id)
        assert fetched.tags["stage"] == "candidate"
        assert fetched.params["max_tokens"] == "128"
        assert fetched.metrics["latency"].step == 2

        found, _ = remote.search_logged_models(
            ["0"], filter_string="status = 'READY' AND metrics.latency < 1"
        )
        assert [item.model_id for item in found] == [model.model_id]
        with pytest.raises(InvalidParameter):
            remote.log_model(
                "0" * 32,
                LinearRegression(alpha=0.1).fit([[1.0], [2.0]], [1.0, 3.0]),
                name="remote-artifact",
            )
        with pytest.raises(InvalidParameter):
            remote.search_logged_models(
                ["0"], filter_string="creation_time = 99999999999999999999999999"
            )
        with pytest.raises(InvalidParameter):
            remote._request(
                "PATCH",
                f"/api/2.0/mlflow/logged-models/{model.model_id}",
                body={},
            )
        with pytest.raises(ModelLoadError):
            remote.load_model(f"models:/{model.model_id}")
        remote.delete_logged_model_tag(model.model_id, "stage")
        remote.delete_logged_model_tag(model.model_id, "owner/team")
        remote.delete_logged_model_tag(model.model_id, "pct%2Fkey")
        remaining = remote.get_logged_model(model.model_id).tags
        assert not {"stage", "owner/team", "pct%2Fkey"} & set(remaining)
        remote.delete_logged_model(model.model_id)
        with pytest.raises(ResourceNotFound):
            remote.get_logged_model(model.model_id)
    finally:
        remote.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_entity_wire_roundtrip() -> None:
    metric = Metric("acc", 0.5, 10, 2)
    model = LoggedModel(
        model_id="m-" + "a" * 32,
        experiment_id="3",
        name="wire",
        artifact_location="file:///tmp/model",
        creation_timestamp=1,
        last_updated_timestamp=2,
        model_type="Agent",
        source_run_id=None,
        status=LoggedModelStatus.FAILED.value,
        status_message="boom",
        tags={"a": "b"},
        params={"p": "1"},
        metrics={"acc": metric},
    )
    decoded = LoggedModel.from_dict(model.to_dict())
    assert decoded.to_dict() == model.to_dict()
    assert decoded.model_uri == model.model_uri
    assert decoded.metrics["acc"].step == 2
