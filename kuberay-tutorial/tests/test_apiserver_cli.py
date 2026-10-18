from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest
import yaml

from minikuberay import constants as C
from minikuberay.apiserver.server import ApiServer
from minikuberay.cli.main import main
from minikuberay.errors import NotFound
from minikuberay.kube.client import HTTPClient
from minikuberay.models.meta import new_object
from tests.conftest import Harness, cluster_spec


@pytest.fixture
def server(harness):
    api = ApiServer(harness.store, runtime=harness.runtime, metrics=harness.operator.metrics.registry, port=0)
    url = api.start()
    yield api, url, harness
    api.stop()


def cluster_doc(name="http-cluster"):
    return new_object(C.RAY_API_VERSION, C.KIND_RAYCLUSTER, name, "default", spec=cluster_spec(workers=1, min_replicas=1))


def test_health_version_and_dashboard(server):
    api, url, harness = server
    client = HTTPClient(url)
    assert client._request("GET", "/healthz")["status"] == "ok"
    assert client._request("GET", "/readyz")["status"] == "ok"
    assert "gitVersion" in client._request("GET", "/version")
    html = client._request("GET", "/dashboard", raw=True).decode()
    assert "MiniKubeRay Dashboard" in html

    # Metrics are only registered after a reconcile; create a cluster first.
    harness.apply(C.KIND_RAYCLUSTER, "metrics-cluster", cluster_spec(workers=1, min_replicas=1))
    harness.settle(20)
    metrics = client._request("GET", "/metrics", raw=True).decode()
    assert "# TYPE kuberay_operator_reconcile_total" in metrics
    assert 'kuberay_raycluster_ready{namespace="default",raycluster="metrics-cluster"} 1' in metrics


def test_http_create_get_list_and_delete(server):
    _, url, _ = server
    client = HTTPClient(url)
    created = client.create(C.KIND_RAYCLUSTER, cluster_doc())
    assert created["metadata"]["name"] == "http-cluster"
    assert created["spec"]["rayVersion"] == "2.59.0"
    fetched = client.get(C.KIND_RAYCLUSTER, "http-cluster", "default")
    assert fetched["metadata"]["uid"] == created["metadata"]["uid"]
    assert len(client.list(C.KIND_RAYCLUSTER, "default")) == 1
    assert len(client.list(C.KIND_RAYCLUSTER, None)) == 1
    client.delete(C.KIND_RAYCLUSTER, "http-cluster", "default", force=True)
    assert client.list(C.KIND_RAYCLUSTER, "default") == []


def test_http_status_subresource(server):
    _, url, _ = server
    client = HTTPClient(url)
    created = client.create(C.KIND_RAYCLUSTER, cluster_doc())
    created["status"] = {"state": "ready"}
    updated = client.update_status(created)
    assert updated["status"]["state"] == "ready"
    assert client.get(C.KIND_RAYCLUSTER, "http-cluster")["status"]["state"] == "ready"


def test_http_patch(server):
    _, url, _ = server
    client = HTTPClient(url)
    client.create(C.KIND_RAYCLUSTER, cluster_doc())
    patched = client._request(
        "PATCH",
        "/apis/ray.io/v1/namespaces/default/rayclusters/http-cluster",
        {"metadata": {"labels": {"team": "ml"}}},
    )
    assert patched["metadata"]["labels"]["team"] == "ml"


def test_http_label_selector(server):
    _, url, _ = server
    client = HTTPClient(url)
    doc = cluster_doc("labelled")
    doc["metadata"]["labels"] = {"team": "ml"}
    client.create(C.KIND_RAYCLUSTER, doc)
    client.create(C.KIND_RAYCLUSTER, cluster_doc("other"))
    selected = client.list(C.KIND_RAYCLUSTER, "default", label_selector={"team": "ml"})
    assert [item["metadata"]["name"] for item in selected] == ["labelled"]


def test_http_watch_stream(server):
    _, url, harness = server
    harness.store.create(C.KIND_RAYCLUSTER, cluster_doc("watched"))
    request = urllib.request.Request(url + "/apis/ray.io/v1/namespaces/default/rayclusters?watch=true")
    with urllib.request.urlopen(request, timeout=3) as response:
        line = response.readline()
    event = json.loads(line.decode())
    assert event["type"] == "ADDED"
    assert event["object"]["metadata"]["name"] == "watched"


def test_http_rayjob_logs_and_stop(server):
    _, url, harness = server
    client = HTTPClient(url)
    job = new_object(
        C.RAY_API_VERSION,
        C.KIND_RAYJOB,
        "logs-job",
        "default",
        spec={"entrypoint": "echo log-line", "shutdownAfterJobFinishes": False, "rayClusterSpec": cluster_spec(workers=0)},
    )
    client.create(C.KIND_RAYJOB, job)
    harness.step(40, advance=0.5)
    body = client._request("GET", "/apis/ray.io/v1/namespaces/default/rayjobs/logs-job/logs?tail=100", raw=True).decode()
    assert "echo log-line" in body or "job log" in body
    result = client._request("POST", "/apis/ray.io/v1/namespaces/default/rayjobs/logs-job/stop", {})
    assert result["status"] in ("stopped",)


def test_auth_token(server):
    api, url, _ = server
    api.token = "secret"
    request = urllib.request.Request(url + "/api/v1/namespaces/default/rayclusters")
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(request, timeout=2)
    assert exc.value.code == 401
    request.add_header("Authorization", "Bearer secret")
    assert urllib.request.urlopen(request, timeout=2).status == 200


def test_read_only_mode(server):
    api, url, _ = server
    api.read_only = True
    cases = [
        ("POST", "/apis/ray.io/v1/namespaces/default/rayclusters", json.dumps(cluster_doc())),
        ("PUT", "/apis/ray.io/v1/namespaces/default/rayclusters/demo", json.dumps(cluster_doc("demo"))),
        ("PATCH", "/apis/ray.io/v1/namespaces/default/rayclusters/demo", json.dumps({"spec": {"rayVersion": "2.60.0"}})),
        ("DELETE", "/apis/ray.io/v1/namespaces/default/rayclusters/demo", None),
    ]
    for method, path, body in cases:
        request = urllib.request.Request(
            url + path,
            data=body.encode() if body else None,
            method=method,
            headers={"Content-Type": "application/json"},
        )
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(request, timeout=2)
        assert exc.value.code == 403, f"{method} should be rejected in read-only mode"


def test_cli_apply_get_scale_delete(server, tmp_path, capsys):
    _, url, harness = server
    path = tmp_path / "cluster.yaml"
    path.write_text(
        yaml.safe_dump(cluster_doc("cli-cluster"), sort_keys=False),
        encoding="utf-8",
    )
    assert main(["apply", "-f", str(path), "--server", url]) == 0
    assert harness.store.exists(C.KIND_RAYCLUSTER, "cli-cluster", "default")
    assert main(["get", "rayclusters", "--server", url]) == 0
    output = capsys.readouterr().out
    assert "cli-cluster" in output
    assert main(["get", "raycluster", "cli-cluster", "-o", "json", "--server", url]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["kind"] == C.KIND_RAYCLUSTER
    assert main(["scale", "raycluster/cli-cluster", "workers=2", "--server", url]) == 0
    assert harness.get(C.KIND_RAYCLUSTER, "cli-cluster")["spec"]["workerGroupSpecs"][0]["replicas"] == 2
    assert main(["delete", "raycluster/cli-cluster", "--force", "--server", url]) == 0
    assert not harness.store.exists(C.KIND_RAYCLUSTER, "cli-cluster", "default")


def test_cli_validate(tmp_path, capsys):
    path = tmp_path / "bad.yaml"
    path.write_text(
        yaml.safe_dump(
            new_object(C.RAY_API_VERSION, C.KIND_RAYCLUSTER, "Bad_Name", "default", spec=cluster_spec(workers=0)),
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    assert main(["validate", "-f", str(path)]) == 1
    output = capsys.readouterr().out
    assert "INVALID" in output


def test_cli_demo(capsys):
    assert main(["demo", "--fast"]) == 0
    output = capsys.readouterr().out
    assert "Demo complete" in output
    assert "RayService" in output


def test_cli_version(capsys):
    assert main(["version"]) == 0
    assert "minikuberay" in capsys.readouterr().out


def test_raycluster_nodes_and_workergroups_subresources(server):
    api, url, harness = server
    harness.apply(C.KIND_RAYCLUSTER, "inspect", cluster_spec(workers=2, min_replicas=2))
    harness.settle(30)
    nodes = HTTPClient(url)._request(
        "GET", "/apis/ray.io/v1/namespaces/default/rayclusters/inspect/nodes"
    )["items"]
    assert len(nodes) == 3
    assert {node["nodeType"] for node in nodes} == {"head", "worker"}
    groups = HTTPClient(url)._request(
        "GET", "/apis/ray.io/v1/namespaces/default/rayclusters/inspect/workergroups"
    )
    assert groups["items"][0]["name"] == "workers"
    assert groups["desiredWorkerReplicas"] == 2


def test_cli_create_cluster_and_submit_job(server, capsys):
    api, url, harness = server
    assert main(["create", "cluster", "cli-cluster", "--worker-replicas", "1", "--server", url]) == 0
    assert harness.get(C.KIND_RAYCLUSTER, "cli-cluster")["spec"]["workerGroupSpecs"][0]["replicas"] == 1
    assert main([
        "job", "submit", "cli-job", "--cluster", "cli-cluster",
        "--entrypoint", "python train.py", "--server", url,
    ]) == 0
    job = harness.get(C.KIND_RAYJOB, "cli-job")
    assert job["spec"]["clusterSelector"][C.LABEL_CLUSTER] == "cli-cluster"
    assert "created" in capsys.readouterr().out


def test_cli_get_nodes_and_workergroups(server, capsys):
    api, url, harness = server
    harness.apply(C.KIND_RAYCLUSTER, "cli-inspect", cluster_spec(workers=1, min_replicas=1))
    harness.settle(30)
    assert main(["get", "nodes", "-c", "cli-inspect", "--server", url]) == 0
    output = capsys.readouterr().out
    assert "cli-inspect-head" in output
    assert main(["get", "workergroups", "-c", "cli-inspect", "--server", url]) == 0
    assert "workers" in capsys.readouterr().out


def test_cli_history_list(server, capsys):
    api, url, harness = server
    harness.apply(C.KIND_RAYCLUSTER, "cli-history", cluster_spec(workers=0))
    harness.settle(20)
    # The fixture server was created before the cluster; attach history now.
    api.history = harness.operator.history
    assert main(["history", "list", "--server", url]) == 0
    assert "cli-history" in capsys.readouterr().out


def test_discovery_and_dry_run(server):
    api, url, harness = server
    client = HTTPClient(url)
    assert client._request("GET", "/api")["versions"] == ["v1"]
    groups = client._request("GET", "/apis")["groups"]
    assert any(group["name"] == "ray.io" for group in groups)

    body = cluster_doc("dry")
    preview = client._request("POST", "/apis/ray.io/v1/namespaces/default/rayclusters?dryRun=All", body)
    assert preview["metadata"]["name"] == "dry"
    assert not harness.store.exists(C.KIND_RAYCLUSTER, "dry", "default")

    client.create(C.KIND_RAYCLUSTER, cluster_doc("real"))
    current = client.get(C.KIND_RAYCLUSTER, "real")
    current["metadata"]["labels"] = {"team": "ml"}
    preview = client._request(
        "PUT",
        "/apis/ray.io/v1/namespaces/default/rayclusters/real?dryRun=All",
        current,
    )
    assert preview["metadata"]["labels"]["team"] == "ml"
    assert client.get(C.KIND_RAYCLUSTER, "real")["metadata"]["labels"].get("team") != "ml"
    preview_delete = client._request(
        "DELETE",
        "/apis/ray.io/v1/namespaces/default/rayclusters/real?dryRun=All",
    )
    assert preview_delete["metadata"]["name"] == "real"
    assert harness.store.exists(C.KIND_RAYCLUSTER, "real", "default")


def test_state_api_subset(server):
    """The /api/v0 teaching subset uses Ray's response envelope."""
    _, url, harness = server
    client = HTTPClient(url)
    harness.apply(C.KIND_RAYCLUSTER, "state-cluster", cluster_spec(workers=2, min_replicas=2))
    harness.settle(40, advance=0.5)

    nodes = client.state("nodes", cluster="state-cluster")["data"]["result"]
    assert nodes["total"] == 3
    assert {node["state"] for node in nodes["result"]} == {"ALIVE"}
    workers = client.state("nodes", cluster="state-cluster", filter="worker")["data"]["result"]
    assert workers["total"] == 2
    assert client.state("healthz") == {"result": True, "msg": ""}

    job = new_object(
        C.RAY_API_VERSION,
        C.KIND_RAYJOB,
        "state-job",
        "default",
        spec={"entrypoint": "echo state", "rayClusterSpec": cluster_spec(workers=0)},
    )
    client.create(C.KIND_RAYJOB, job)
    harness.step(30, advance=0.5)
    jobs = client.state("jobs", namespace="default")["data"]["result"]
    assert jobs["total"] >= 1
    assert any(item["jobId"] for item in jobs["result"])

    # Unknown State API dataset must fail loudly rather than returning [].
    with pytest.raises(NotFound):
        client.state("tasks")

    # CLI renders the same dataset as a table.
    assert main(["state", "nodes", "--cluster", "state-cluster", "--server", url]) == 0


def test_http_rejects_malformed_ray_spec_with_422(server):
    _, url, _ = server
    client = HTTPClient(url)
    body = cluster_doc("malformed")
    body["spec"]["workerGroupSpecs"] = ["oops"]
    with pytest.raises(Exception) as excinfo:
        client.create(C.KIND_RAYCLUSTER, body)
    assert getattr(excinfo.value, "status", None) == 422


def test_list_envelope_uses_group_version(server):
    _, url, harness = server
    client = HTTPClient(url)
    harness.store.create(
        C.KIND_JOB,
        new_object("batch/v1", C.KIND_JOB, "j", "default", spec={"template": {"spec": {"containers": [{"name": "c", "image": "x"}]}}}),
        "default",
    )
    listed = client._request("GET", "/apis/batch/v1/namespaces/default/jobs")
    assert listed["apiVersion"] == "batch/v1"
