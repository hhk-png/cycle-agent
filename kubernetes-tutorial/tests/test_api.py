from __future__ import annotations

import threading
import time

import pytest

from mini_k8s.client import ApiClient
from mini_k8s.errors import ApiError


def configmap(name="cfg", value="v", namespace="default"):
    return {"apiVersion": "v1", "kind": "ConfigMap",
            "metadata": {"name": name, "namespace": namespace}, "data": {"k": value}}


def test_discovery_endpoints(api_cluster):
    assert api_cluster.api.request("GET", "/api")["versions"] == ["v1"]
    groups = api_cluster.api.request("GET", "/apis")["groups"]
    assert any(group["name"] == "apps" for group in groups)
    apps = api_cluster.api.request("GET", "/apis/apps/v1")["resources"]
    assert any(item["kind"] == "Deployment" and item["name"] == "deployments" for item in apps)
    assert api_cluster.api.request("GET", "/version")["gitVersion"].startswith("v1.31")


def test_create_get_list_delete(api_cluster):
    created = api_cluster.api.create(configmap())
    assert created["metadata"]["uid"]
    fetched = api_cluster.api.get("v1", "ConfigMap", "cfg")
    assert fetched["data"]["k"] == "v"
    listing = api_cluster.api.list("v1", "ConfigMap", "default")
    assert [item["metadata"]["name"] for item in listing["items"]] == ["cfg"]
    api_cluster.api.delete("v1", "ConfigMap", "cfg")
    with pytest.raises(ApiError) as exc:
        api_cluster.api.get("v1", "ConfigMap", "cfg")
    assert exc.value.status == 404


def test_duplicate_create_returns_409(api_cluster):
    api_cluster.api.create(configmap())
    with pytest.raises(ApiError) as exc:
        api_cluster.api.create(configmap())
    assert exc.value.status == 409


def test_list_label_selector(api_cluster):
    for index in range(3):
        body = configmap(f"c{index}")
        body["metadata"]["labels"] = {"index": str(index), "kind": "test"}
        api_cluster.api.create(body)
    result = api_cluster.api.list("v1", "ConfigMap", "default", label_selector="index in (0,2)")
    assert {item["metadata"]["name"] for item in result["items"]} == {"c0", "c2"}


def test_list_pagination(api_cluster):
    for index in range(5):
        api_cluster.api.create(configmap(f"c{index}"))
    first = api_cluster.api.list("v1", "ConfigMap", "default", limit=2)
    assert len(first["items"]) == 2 and first["metadata"]["continue"]
    second = api_cluster.api.list("v1", "ConfigMap", "default", limit=2,
                                  continue_token=first["metadata"]["continue"])
    assert len(second["items"]) == 2


def test_merge_and_json_patch(api_cluster):
    api_cluster.api.create(configmap())
    merged = api_cluster.api.patch("v1", "ConfigMap", "cfg", {"data": {"k": "new", "extra": "x"}})
    assert merged["data"] == {"k": "new", "extra": "x"}
    patched = api_cluster.api.patch(
        "v1", "ConfigMap", "cfg",
        [{"op": "replace", "path": "/data/k", "value": "json"}],
        content_type="application/json-patch+json")
    assert patched["data"]["k"] == "json"


def test_put_requires_resource_version(api_cluster):
    api_cluster.api.create(configmap())
    body = {"apiVersion": "v1", "kind": "ConfigMap",
            "metadata": {"name": "cfg", "namespace": "default"}, "data": {"k": "new"}}
    with pytest.raises(ApiError) as exc:
        api_cluster.api.request("PUT", "/api/v1/namespaces/default/configmaps/cfg", body)
    assert exc.value.status == 422
    assert api_cluster.api.get("v1", "ConfigMap", "cfg")["data"]["k"] == "v"


def test_resource_version_conflict(api_cluster):
    created = api_cluster.api.create(configmap())
    stale = dict(created)
    fresh = api_cluster.api.get("v1", "ConfigMap", "cfg")
    fresh["data"]["k"] = "fresh"
    api_cluster.api.replace(fresh)
    stale["data"]["k"] = "stale"
    with pytest.raises(ApiError) as exc:
        api_cluster.api.replace(stale)
    assert exc.value.status == 409


def test_watch_stream(api_cluster):
    rv = api_cluster.api.list("v1", "ConfigMap", "default")["metadata"]["resourceVersion"]
    events = []

    def consume():
        for event in api_cluster.api.watch("v1", "ConfigMap", "default",
                                           resource_version=rv, timeout_seconds=3):
            events.append(event)
            break

    thread = threading.Thread(target=consume)
    thread.start()
    time.sleep(0.2)
    api_cluster.api.create(configmap())
    thread.join(timeout=5)
    assert events and events[0]["type"] == "ADDED"
    assert events[0]["object"]["metadata"]["name"] == "cfg"


def test_status_subresource_and_scale(api_cluster, deploy):
    api_cluster.api.apply(deploy("web", replicas=1))
    api_cluster.wait_for_deployment("web")
    scale = api_cluster.api.scale("apps/v1", "Deployment", "web", None)
    assert scale["spec"]["replicas"] == 1
    scale = api_cluster.api.scale("apps/v1", "Deployment", "web", 3)
    assert scale["spec"]["replicas"] == 3
    api_cluster.wait_for_deployment("web")
    status_path = api_cluster.api.path_for("apps/v1", "Deployment", "default", "web", "status")
    status = api_cluster.api.request("GET", status_path)["status"]
    assert status["readyReplicas"] == 3

    # Writing status must not bump metadata.generation; otherwise every
    # controller status update would look like a new spec revision.
    before = api_cluster.get("deployments", "web")["metadata"]["generation"]
    api_cluster.api.request("PUT", status_path, {
        "apiVersion": "apps/v1", "kind": "Deployment",
        "metadata": {"name": "web", "namespace": "default"},
        "status": {**status, "observedGeneration": before},
    })
    after = api_cluster.get("deployments", "web")["metadata"]["generation"]
    assert after == before


def test_logs_and_exec_endpoints(api_cluster):
    pod = {"apiVersion": "v1", "kind": "Pod",
           "metadata": {"name": "shell", "namespace": "default"},
           "spec": {"containers": [{"name": "c", "image": "busybox"}]}}
    api_cluster.api.create(pod)
    api_cluster.wait_for_pod("shell")
    text = api_cluster.api.logs("shell")
    assert "Starting container c" in text
    result = api_cluster.api.exec("shell", ["echo", "api"])
    assert result["stdout"] == "api\n"


def test_authentication_required(api_cluster):
    anonymous = ApiClient(api_cluster.url, token=None)
    with pytest.raises(ApiError) as exc:
        anonymous.list("v1", "Pod", "default")
    assert exc.value.status == 403
    invalid = ApiClient(api_cluster.url, token="not-a-token")
    with pytest.raises(ApiError) as exc:
        invalid.list("v1", "Pod", "default")
    assert exc.value.status == 401


def test_dry_run_does_not_persist(api_cluster):
    result = api_cluster.api.request(
        "POST", "/api/v1/namespaces/default/configmaps?dryRun=All", configmap("dry"))
    assert result["metadata"]["name"] == "dry"
    with pytest.raises(ApiError):
        api_cluster.api.get("v1", "ConfigMap", "dry")


def test_delete_with_finalizer_returns_terminating(api_cluster):
    body = configmap("final")
    body["metadata"]["finalizers"] = ["example.com/hold"]
    api_cluster.api.create(body)
    deleted = api_cluster.api.delete("v1", "ConfigMap", "final")
    assert deleted["metadata"]["deletionTimestamp"]
    current = api_cluster.api.get("v1", "ConfigMap", "final")
    current["metadata"]["finalizers"] = []
    # Clearing the last finalizer through an update completes deletion.
    api_cluster.api.replace(current)
    with pytest.raises(ApiError) as exc:
        api_cluster.api.get("v1", "ConfigMap", "final")
    assert exc.value.status == 404
    with pytest.raises(ApiError):
        api_cluster.api.get("v1", "ConfigMap", "final")


def test_access_review(api_cluster):
    result = api_cluster.api.request(
        "POST", "/apis/authorization.k8s.io/v1/selfsubjectaccessreviews",
        {"apiVersion": "authorization.k8s.io/v1", "kind": "SelfSubjectAccessReview",
         "spec": {"resourceAttributes": {"verb": "get", "resource": "pods", "namespace": "default"}}})
    assert result["status"]["allowed"] is True


def test_token_review_enforces_audience(api_cluster):
    issued = api_cluster.token_issuer.issue("default", "builder", audiences=["aud-a"])
    body = {"apiVersion": "authentication.k8s.io/v1", "kind": "TokenReview",
            "spec": {"token": issued["token"], "audiences": ["aud-b"]}}
    result = api_cluster.api.request("POST", "/apis/authentication.k8s.io/v1/tokenreviews", body)
    assert result["status"]["authenticated"] is False

    body["spec"]["audiences"] = ["aud-a"]
    result = api_cluster.api.request("POST", "/apis/authentication.k8s.io/v1/tokenreviews", body)
    assert result["status"]["authenticated"] is True
    assert result["status"]["user"]["username"] == "system:serviceaccount:default:builder"


def test_label_selector_watch_synthesizes_added_and_deleted(api_cluster):
    body = configmap("cm-labels")
    body["metadata"]["labels"] = {"app": "watched"}
    api_cluster.api.create(body)
    rv = str(api_cluster.store.resource_version)
    events: list[dict] = []

    def collect() -> None:
        for event in api_cluster.api.watch("v1", "ConfigMap", "default",
                                           resource_version=rv, timeout_seconds=2,
                                           label_selector="app=watched"):
            events.append(event)

    thread = threading.Thread(target=collect, daemon=True)
    thread.start()
    time.sleep(0.3)
    api_cluster.api.patch("v1", "ConfigMap", "cm-labels", {"metadata": {"labels": {"app": "other"}}})
    time.sleep(0.3)
    api_cluster.api.patch("v1", "ConfigMap", "cm-labels", {"metadata": {"labels": {"app": "watched"}}})
    thread.join(timeout=4)
    types = [event["type"] for event in events]
    assert "DELETED" in types, events
    assert "ADDED" in types, events
