from __future__ import annotations

import time

import pytest

from mini_k8s.client import ApiClient
from mini_k8s.errors import ApiError


def test_namespace_deletion_cascades(cluster):
    cluster.apply({"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": "temp"}})
    time.sleep(0.2)
    cluster.apply({"apiVersion": "v1", "kind": "ConfigMap",
                   "metadata": {"name": "cm", "namespace": "temp"}, "data": {"a": "b"}})
    cluster.apply({"apiVersion": "v1", "kind": "Pod",
                   "metadata": {"name": "p", "namespace": "temp"},
                   "spec": {"containers": [{"name": "c", "image": "x"}]}})
    cluster.delete({"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": "temp"}})
    cluster.wait_until(lambda: cluster.get("namespaces", "temp") is None,
                       timeout=10, description="namespace deleted")
    assert cluster.get("configmaps", "cm", "temp") is None
    assert cluster.get("pods", "p", "temp") is None


def test_garbage_collector_removes_dependents(cluster, deploy):
    cluster.apply(deploy("web", replicas=2))
    cluster.wait_for_deployment("web")
    assert cluster.list("replicasets")
    assert cluster.list("pods")
    cluster.delete({"apiVersion": "apps/v1", "kind": "Deployment",
                    "metadata": {"name": "web", "namespace": "default"}})
    cluster.wait_until(lambda: not cluster.list("replicasets") and not cluster.list("pods"),
                       timeout=8, description="garbage collection")
    assert not cluster.list("replicasets")


def test_quota_status_updated(cluster):
    cluster.apply({"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": "q"}})
    cluster.apply({"apiVersion": "v1", "kind": "ResourceQuota",
                   "metadata": {"name": "quota", "namespace": "q"},
                   "spec": {"hard": {"pods": "3"}}})
    cluster.apply({"apiVersion": "v1", "kind": "Pod",
                   "metadata": {"name": "one", "namespace": "q"},
                   "spec": {"containers": [{"name": "c", "image": "x"}]}})
    cluster.wait_until(lambda: (cluster.get("resourcequotas", "quota", "q") or {}).get("status", {}).get("used", {}).get("pods") == "1",
                       timeout=5, description="quota status")
    assert cluster.get("resourcequotas", "quota", "q")["status"]["hard"]["pods"] == "3"


def test_quota_uses_effective_init_request(cluster):
    cluster.apply({"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": "q2"}})
    cluster.apply({"apiVersion": "v1", "kind": "ResourceQuota",
                   "metadata": {"name": "cpu-quota", "namespace": "q2"},
                   "spec": {"hard": {"requests.cpu": "500m", "pods": "2"}}})
    cluster.apply({"apiVersion": "v1", "kind": "Pod",
                   "metadata": {"name": "init-heavy", "namespace": "q2"},
                   "spec": {
                       "initContainers": [{"name": "init", "image": "x",
                                           "resources": {"requests": {"cpu": "400m"}}}],
                       "containers": [{"name": "c", "image": "x",
                                       "resources": {"requests": {"cpu": "200m"}}}],
                   }})
    assert cluster.get("pods", "init-heavy", "q2") is not None
    cluster.wait_until(
        lambda: (cluster.get("resourcequotas", "cpu-quota", "q2") or {}).get("status", {}).get("used", {}).get("requests.cpu") == "400m",
        timeout=5, description="quota effective request")


def test_pdb_blocks_eviction(api_cluster, deploy):
    api_cluster.apply(deploy("quorum", replicas=3))
    api_cluster.wait_for_deployment("quorum")
    api_cluster.apply({"apiVersion": "policy/v1", "kind": "PodDisruptionBudget",
                   "metadata": {"name": "quorum", "namespace": "default"},
                   "spec": {"minAvailable": 3, "selector": {"matchLabels": {"app": "quorum"}}}})
    api_cluster.wait_until(lambda: (api_cluster.get("poddisruptionbudgets", "quorum") or {}).get("status", {}).get("disruptionsAllowed") == 0,
                       timeout=5, description="pdb status")
    pod = api_cluster.list("pods")[0]
    with pytest.raises(ApiError) as exc:
        api_cluster.api.evict(pod["metadata"]["name"])
    assert exc.value.status == 429
    assert "disruption budget" in exc.value.message


def test_rbac_denies_then_allows(api_cluster):
    developer = ApiClient(api_cluster.url, token="mini-dev-token")
    with pytest.raises(ApiError) as exc:
        developer.list("v1", "Pod", "default")
    assert exc.value.status == 403
    api_cluster.apply({"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "Role",
                       "metadata": {"name": "reader", "namespace": "default"},
                       "rules": [{"apiGroups": [""], "resources": ["pods", "pods/log"],
                                  "verbs": ["get", "list"]}]})
    api_cluster.apply({"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "RoleBinding",
                       "metadata": {"name": "read", "namespace": "default"},
                       "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": "reader"},
                       "subjects": [{"kind": "Group", "name": "developers"}]})
    time.sleep(0.1)
    assert developer.list("v1", "Pod", "default")["items"] == []
    allowed, _ = developer.can_i("get", "pods")
    assert allowed
    denied, _ = developer.can_i("delete", "pods")
    assert not denied


def test_rbac_resource_names_and_subresources(api_cluster):
    api_cluster.apply({"apiVersion": "v1", "kind": "Pod",
                       "metadata": {"name": "visible", "namespace": "default"},
                       "spec": {"containers": [{"name": "c", "image": "x"}]}})
    api_cluster.apply({"apiVersion": "v1", "kind": "Pod",
                       "metadata": {"name": "hidden", "namespace": "default"},
                       "spec": {"containers": [{"name": "c", "image": "x"}]}})
    api_cluster.apply({"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "Role",
                       "metadata": {"name": "one-pod", "namespace": "default"},
                       "rules": [{"apiGroups": [""], "resources": ["pods"], "resourceNames": ["visible"],
                                  "verbs": ["get"]},
                                 {"apiGroups": [""], "resources": ["pods/log"], "verbs": ["get"]}]})
    api_cluster.apply({"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "RoleBinding",
                       "metadata": {"name": "one-pod", "namespace": "default"},
                       "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": "one-pod"},
                       "subjects": [{"kind": "Group", "name": "developers"}]})
    developer = ApiClient(api_cluster.url, token="mini-dev-token")
    assert developer.get("v1", "Pod", "visible")["metadata"]["name"] == "visible"
    with pytest.raises(ApiError) as exc:
        developer.get("v1", "Pod", "hidden")
    assert exc.value.status == 403
    assert isinstance(developer.logs("visible", "default"), str)


def test_service_account_token_and_review(api_cluster):
    response = api_cluster.api.request(
        "POST", "/api/v1/namespaces/default/serviceaccounts/default/token",
        {"apiVersion": "authentication.k8s.io/v1", "kind": "TokenRequest",
         "spec": {"audiences": ["mini"], "expirationSeconds": 600}})
    token = response["status"]["token"]
    assert token.count(".") == 2
    review = api_cluster.api.request(
        "POST", "/apis/authentication.k8s.io/v1/tokenreviews",
        {"apiVersion": "authentication.k8s.io/v1", "kind": "TokenReview",
         "spec": {"token": token, "audiences": ["mini"]}})
    assert review["status"]["authenticated"]
    assert review["status"]["user"]["username"] == "system:serviceaccount:default:default"
    assert "system:serviceaccounts:default" in review["status"]["user"]["groups"]
    service_account_client = ApiClient(api_cluster.url, token=token)
    with pytest.raises(ApiError) as exc:
        service_account_client.list("v1", "Pod", "default")
    assert exc.value.status == 403


def test_node_heartbeat_marks_not_ready():
    from mini_k8s.cluster import MiniCluster
    cluster = MiniCluster(start_api=False)
    cluster.start()
    try:
        controller = next(c for c in cluster.controllers if c.name == "node-lifecycle-controller")
        controller.grace_seconds = 0.1
        cluster.kubelets["mini-node-1"].stop()
        time.sleep(0.5)
        controller.reconcile()
        node = cluster.get("nodes", "mini-node-1")
        ready = next(c for c in node["status"]["conditions"] if c["type"] == "Ready")
        assert ready["status"] == "False"
        assert any(t["key"] == "node.kubernetes.io/not-ready" for t in node["spec"]["taints"])
    finally:
        cluster.stop()


def test_crd_cleanup_deletes_custom_resources(cluster):
    cluster.apply({"apiVersion": "apiextensions.k8s.io/v1", "kind": "CustomResourceDefinition",
                   "metadata": {"name": "gadgets.example.com"},
                   "spec": {"group": "example.com", "scope": "Namespaced",
                            "names": {"plural": "gadgets", "singular": "gadget", "kind": "Gadget"},
                            "versions": [{"name": "v1", "served": True, "storage": True,
                                          "schema": {"openAPIV3Schema": {"type": "object"}}}]}})
    cluster.apply({"apiVersion": "example.com/v1", "kind": "Gadget",
                   "metadata": {"name": "g1", "namespace": "default"}, "spec": {"x": 1}})
    assert cluster.get("gadgets", "g1") is not None
    cluster.delete({"apiVersion": "apiextensions.k8s.io/v1", "kind": "CustomResourceDefinition",
                    "metadata": {"name": "gadgets.example.com"}})
    cluster.wait_until(lambda: cluster.get("gadgets", "g1") is None and cluster.get("customresourcedefinitions", "gadgets.example.com") is None,
                       timeout=8, description="crd cleanup")
