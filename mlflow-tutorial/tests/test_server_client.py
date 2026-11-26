from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote

import pytest

import mini_mlflow as mlflow
from mini_mlflow.client import MiniClient
from mini_mlflow.config import MiniConfig
from mini_mlflow.entities import FileInfo, Span, Trace
from mini_mlflow.errors import InvalidParameter, ResourceNotFound, Unauthorized
from mini_mlflow.models import LinearRegression
from mini_mlflow.server import create_server


@pytest.fixture()
def running_server(config: MiniConfig):
    server_config = config.derive(auth_token="secret-token", cors_origin="https://example.test")
    server = create_server(server_config, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    try:
        yield f"http://{host}:{port}", server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture()
def remote(running_server):
    base, _ = running_server
    client = MiniClient(base, auth_token="secret-token")
    try:
        yield client
    finally:
        client.close()


def test_health_without_auth(running_server) -> None:
    base, _ = running_server
    with urllib.request.urlopen(base + "/health") as response:
        payload = json.loads(response.read())
    assert payload["status"] == "ok"
    assert payload["auth_required"] is True


def test_auth_required(running_server) -> None:
    base, _ = running_server
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(base + "/api/2.0/mlflow/experiments/get?experiment_id=0")
    assert excinfo.value.code == 401
    with pytest.raises(Unauthorized):
        MiniClient(base).get_experiment("0")
    with pytest.raises(Unauthorized):
        MiniClient(base, auth_token="wrong").get_experiment("0")


def test_remote_experiment_roundtrip(remote) -> None:
    experiment = remote.create_experiment("remote-exp", tags={"team": "a"})
    assert experiment.name == "remote-exp"
    assert remote.get_experiment(experiment.experiment_id).name == "remote-exp"
    assert remote.get_experiment_by_name("remote-exp").experiment_id == experiment.experiment_id
    assert remote.get_experiment_by_name("missing") is None
    remote.set_experiment_tag(experiment.experiment_id, "owner", "me")
    assert remote.get_experiment(experiment.experiment_id).tags["owner"] == "me"
    experiments, token = remote.search_experiments("tags.team = 'a'")
    assert [item.name for item in experiments] == ["remote-exp"]
    remote.delete_experiment(experiment.experiment_id)
    assert remote.get_experiment(experiment.experiment_id).lifecycle_stage == "deleted"
    remote.restore_experiment(experiment.experiment_id)
    assert remote.get_experiment(experiment.experiment_id).lifecycle_stage == "active"


def test_remote_run_and_search(remote) -> None:
    experiment = remote.create_experiment("runs")
    run = remote.start_run(experiment_id=experiment.experiment_id, run_name="remote-run")
    rid = run.info.run_id
    remote.log_params(rid, {"model": "linear", "lr": 0.1})
    remote.log_metric(rid, "score", 0.5, step=1)
    remote.log_metric(rid, "score", 0.9, step=2)
    remote.set_tag(rid, "stage", "prod")
    remote.start_run(experiment_id=experiment.experiment_id, run_name="second-run")
    history = remote.get_metric_history(rid, "score")
    assert [item.value for item in history] == [0.5, 0.9]
    assert remote.get_run(rid).data.metrics["score"] == 0.9
    runs, _ = remote.search_runs('metrics.score > 0.8 and tags.stage = "prod"')
    assert [item.info.run_id for item in runs] == [rid]
    page1, token = remote.search_runs(max_results=1)
    assert len(page1) == 1 and token
    remote.end_run(rid, "FINISHED")
    assert remote.get_run(rid).info.status == "FINISHED"
    with pytest.raises(InvalidParameter):
        remote.log_metric(rid, "score", 0.1)


def test_remote_artifacts(remote, tmp_path: Path) -> None:
    run = remote.start_run(run_name="artifacts")
    source = tmp_path / "data.txt"
    source.write_text("payload", encoding="utf-8")
    remote.log_artifact(run.info.run_id, source, "docs")
    remote.log_text(run.info.run_id, "text", "docs/note.md")
    files = remote.list_artifacts(run.info.run_id, "docs")
    assert {item.path for item in files} == {"docs/data.txt", "docs/note.md"}
    assert remote.read_artifact(run.info.run_id, "docs/data.txt") == b"payload"
    destination = tmp_path / "download"
    remote.download_artifacts(run.info.run_id, "docs", destination)
    assert (destination / "data.txt").read_text(encoding="utf-8") == "payload"
    with pytest.raises(InvalidParameter):
        remote.log_text(run.info.run_id, "x", "../escape.txt")


def test_remote_model_and_registry(remote) -> None:
    run = remote.start_run(run_name="model")
    model = LinearRegression().fit([{"x": 1.0}, {"x": 2.0}], [2.0, 4.0])
    info = remote.log_model(
        run.info.run_id,
        model,
        input_example=[{"x": 3.0}],
        registered_model_name="remote-model",
    )
    remote.end_run(run.info.run_id)
    assert info.version == 1
    loaded = remote.load_model(info.model_uri)
    assert loaded.predict([{"x": 5.0}]) == pytest.approx([10.0])
    registry = remote.get_registered_model("remote-model")
    assert registry.name == "remote-model"
    remote.set_registered_model_alias("remote-model", "champion", 1)
    assert remote.get_model_version_by_alias("remote-model", "champion").version == 1
    by_alias = remote.load_model("models:/remote-model@champion")
    assert by_alias.predict([{"x": 3.0}]) == pytest.approx([6.0])
    remote.transition_model_version_stage("remote-model", 1, "production")
    assert remote.get_model_version("remote-model", 1).stage == "Production"
    versions = remote.search_model_versions(name="remote-model")
    assert versions[0].version == 1
    remote.set_model_version_tag("remote-model", 1, "approved", "true")
    assert remote.get_model_version("remote-model", 1).tags["approved"] == "true"
    remote.delete_registered_model_alias("remote-model", "champion")
    with pytest.raises(ResourceNotFound):
        remote.get_model_version_by_alias("remote-model", "champion")


def test_remote_trace(remote) -> None:
    trace = Trace(
        trace_id="d" * 32,
        name="remote-trace",
        experiment_id="0",
        tags={"env": "test"},
        spans=[
            Span(
                span_id="e" * 16,
                trace_id="d" * 32,
                name="root",
                start_time_ns=1,
                end_time_ns=100,
                inputs={"q": "hi"},
                outputs={"a": "ok"},
            )
        ],
    )
    saved = remote.log_trace(trace)
    assert saved.name == "remote-trace"
    assert remote.get_trace("d" * 32).response == {"a": "ok"}
    traces = remote.search_traces(filter_string='tags.env = "test"')
    assert len(traces) == 1


def test_ui_escapes_and_serves(running_server) -> None:
    base, server = running_server
    service = server.service
    service.create_experiment("<script>alert(1)</script>", tags={"xss": "<img src=x onerror=alert(1)>"})
    request = urllib.request.Request(base + "/ui?token=secret-token")
    with urllib.request.urlopen(request) as response:
        html = response.read().decode("utf-8")
        cookie = response.headers.get("Set-Cookie")
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "<img src=x onerror=alert(1)>" not in html
    assert cookie.startswith("mini_mlflow_token=secret-token")
    cookie_request = urllib.request.Request(
        base + "/ui", headers={"Cookie": "mini_mlflow_token=secret-token"}
    )
    with urllib.request.urlopen(cookie_request) as response:
        assert "MiniMLflow" in response.read().decode("utf-8")


def test_api_error_shapes_and_unknown_route(remote, running_server) -> None:
    base, _ = running_server
    with pytest.raises(ResourceNotFound):
        remote.get_experiment("999999")
    with pytest.raises(ResourceNotFound):
        remote.get_run("f" * 32)
    request = urllib.request.Request(
        base + "/api/2.0/mlflow/does-not-exist",
        headers={"Authorization": "Bearer secret-token"},
    )
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(request)
    payload = json.loads(excinfo.value.read())
    assert payload["error_code"] == "RESOURCE_DOES_NOT_EXIST"

    bad = urllib.request.Request(
        base + "/api/2.0/mlflow/experiments/search",
        data=b"{not json}",
        headers={"Authorization": "Bearer secret-token", "Content-Type": "application/json"},
        method="POST",
    )
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(bad)
    assert excinfo.value.code == 400


def test_cors_headers(running_server) -> None:
    base, _ = running_server
    request = urllib.request.Request(base + "/health", method="OPTIONS")
    with urllib.request.urlopen(request) as response:
        assert response.status == 204
        assert response.headers["Access-Control-Allow-Origin"] == "https://example.test"


def test_remote_log_batch_and_delete_tag(remote) -> None:
    run = remote.start_run()
    remote.log_batch(
        run.info.run_id,
        metrics=[{"key": "a", "value": 1.0, "step": 1}],
        params={"p": "v"},
        tags={"t": "v"},
    )
    remote.delete_tag(run.info.run_id, "t")
    stored = remote.get_run(run.info.run_id)
    assert stored.data.metrics["a"] == 1.0
    assert stored.data.params["p"] == "v"
    assert "t" not in stored.data.tags


def test_remote_already_exists_uses_matching_exception(remote) -> None:
    from mini_mlflow.errors import ResourceAlreadyExists

    remote.create_experiment("dupe")
    with pytest.raises(ResourceAlreadyExists):
        remote.create_experiment("dupe")


def test_auth_cookie_supports_special_characters(config: MiniConfig) -> None:
    token = "a;b/c+d"
    server = create_server(config.derive(auth_token=token), host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        request = urllib.request.Request(base + "/ui?token=" + quote(token, safe=""))
        with urllib.request.urlopen(request) as response:
            cookie = response.headers["Set-Cookie"].split(";", 1)[0]
            assert "%" in cookie or "+" in cookie
        cookie_request = urllib.request.Request(base + "/ui", headers={"Cookie": cookie})
        with urllib.request.urlopen(cookie_request) as response:
            assert response.status == 200
        api = MiniClient(base, auth_token=token)
        assert api.get_experiment("0").name == "Default"
        api.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_cookie_auth_cannot_mutate(running_server) -> None:
    base, _ = running_server
    with urllib.request.urlopen(base + "/ui?token=secret-token") as response:
        cookie = response.headers["Set-Cookie"].split(";", 1)[0]
    request = urllib.request.Request(
        base + "/api/2.0/mlflow/experiments/create",
        data=json.dumps({"name": "csrf-attempt"}).encode("utf-8"),
        headers={"Cookie": cookie, "Content-Type": "application/json"},
        method="POST",
    )
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(request)
    assert excinfo.value.code == 401


def test_unicode_artifact_names_are_downloadable(remote) -> None:
    run = remote.start_run(run_name="unicode-artifact")
    remote.log_text(run.info.run_id, "hello", "报告/中文.txt")
    assert remote.read_artifact(run.info.run_id, "报告/中文.txt") == b"hello"
    files = remote.list_artifacts(run.info.run_id, "报告")
    assert files[0].path.endswith("中文.txt")
    with pytest.raises(InvalidParameter):
        remote.log_text(run.info.run_id, "x", "bad\nname.txt")


def test_remote_download_rejects_unsafe_server_paths(remote, tmp_path: Path, monkeypatch) -> None:
    run = remote.start_run()
    monkeypatch.setattr(
        remote,
        "list_artifacts",
        lambda run_id, path=None: [
            FileInfo(path=".." + chr(92) + "evil.txt", is_dir=False, file_size=1)
        ],
    )
    with pytest.raises(InvalidParameter, match="unsafe"):
        remote.download_artifacts(run.info.run_id, None, tmp_path / "out")


def test_remote_search_scopes_experiment_ids(remote) -> None:
    exp1 = remote.create_experiment("scope-1")
    exp2 = remote.create_experiment("scope-2")
    run1 = remote.start_run(experiment_id=exp1.experiment_id)
    remote.start_run(experiment_id=exp2.experiment_id)
    runs, _ = remote.search_runs(experiment_ids=[exp1.experiment_id])
    assert [run.info.run_id for run in runs] == [run1.info.run_id]
    with pytest.raises(InvalidParameter):
        remote.search_runs(experiment_ids="not-a-list")


def test_remote_include_deleted_get(remote) -> None:
    run = remote.start_run(run_name="deleted-run")
    remote.delete_run(run.info.run_id)
    with pytest.raises(ResourceNotFound):
        remote.get_run(run.info.run_id)
    assert remote.get_run(run.info.run_id, include_deleted=True).info.lifecycle_stage == "deleted"


def test_malformed_api_inputs_are_4xx(remote, running_server) -> None:
    base, _ = running_server
    headers = {"Authorization": "Bearer secret-token", "Content-Type": "application/json"}
    cases = [
        ("/api/2.0/mlflow/experiments/create", {"name": "bad-artifact", "artifact_location": 123}),
        ("/api/2.0/mlflow/runs/update", {"run_id": "0" * 32, "status": []}),
        ("/api/2.0/mlflow/traces/search", {"experiment_ids": 1}),
    ]
    for path, payload in cases:
        request = urllib.request.Request(
            base + path, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST"
        )
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            urllib.request.urlopen(request)
        assert excinfo.value.code == 400, (path, excinfo.value.code)


def test_remote_model_version_has_history_tag(remote) -> None:
    run = remote.start_run()
    model = LinearRegression().fit([{"x": 1.0}, {"x": 2.0}], [2.0, 4.0])
    info = remote.log_model(run.info.run_id, model, registered_model_name="history-model")
    version = remote.get_model_version("history-model", info.version)
    assert "mlflow.log-model.history" in version.tags


def test_remote_parameter_validation_matches_local(remote, tmp_path: Path) -> None:
    run = remote.start_run()
    remote.log_params(run.info.run_id, {"flag": False})
    assert remote.get_run(run.info.run_id).data.params["flag"] == "false"
    with pytest.raises(InvalidParameter):
        remote.log_params(run.info.run_id, {"bad": None})
    with pytest.raises(InvalidParameter):
        remote.log_params(run.info.run_id, {"nested": {"a": 1}})


def test_non_ascii_bearer_is_401_not_500(running_server) -> None:
    import http.client
    from urllib.parse import urlparse

    base, _ = running_server
    parsed = urlparse(base)
    connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=5)
    connection.request(
        "POST",
        "/api/2.0/mlflow/experiments/create",
        body=json.dumps({"name": "x"}).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer café",
        },
    )
    response = connection.getresponse()
    response.read()
    assert response.status == 401
    assert response.getheader("Connection") == "close"
    connection.close()


def test_keepalive_after_auth_failure_is_clean(running_server) -> None:
    import http.client
    from urllib.parse import urlparse

    base, _ = running_server
    parsed = urlparse(base)
    connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=5)
    body = json.dumps({"name": "unauthorized"}).encode("utf-8")
    connection.request(
        "POST",
        "/api/2.0/mlflow/experiments/create",
        body=body,
        headers={"Content-Type": "application/json", "Content-Length": str(len(body))},
    )
    response = connection.getresponse()
    response.read()
    assert response.status == 401
    connection.close()

    fresh = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=5)
    fresh.request("GET", "/health")
    healthy = fresh.getresponse()
    payload = json.loads(healthy.read())
    assert healthy.status == 200 and payload["status"] == "ok"
    fresh.close()


def test_head_requires_auth_except_health(running_server) -> None:
    import http.client
    from urllib.parse import urlparse

    base, _ = running_server
    parsed = urlparse(base)
    for path, expected in [("/health", 200), ("/api/2.0/mlflow/runs/get", 401)]:
        connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=5)
        connection.request("HEAD", path)
        response = connection.getresponse()
        response.read()
        assert response.status == expected
        connection.close()
