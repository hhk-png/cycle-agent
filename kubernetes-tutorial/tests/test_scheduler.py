from __future__ import annotations

import time

import pytest

from mini_k8s.cluster import MiniCluster


@pytest.fixture
def small_cluster():
    instance = MiniCluster(start_api=False, node_cpu="500m")
    instance.start()
    try:
        yield instance
    finally:
        instance.stop()


def pod(name, *, requests=None, node_selector=None, tolerations=None, affinity=None, host_port=None):
    container = {"name": "c", "image": "x"}
    if requests:
        container["resources"] = {"requests": requests}
    if host_port:
        container["ports"] = [{"containerPort": 80, "hostPort": host_port}]
    spec = {"containers": [container]}
    if node_selector:
        spec["nodeSelector"] = node_selector
    if tolerations:
        spec["tolerations"] = tolerations
    if affinity:
        spec["affinity"] = affinity
    return {"apiVersion": "v1", "kind": "Pod",
            "metadata": {"name": name, "namespace": "default"}, "spec": spec}


def test_resource_fit_leaves_pod_pending(small_cluster):
    small_cluster.apply(pod("big", requests={"cpu": "400m"}))
    small_cluster.wait_for_pod("big")
    small_cluster.apply(pod("too-big", requests={"cpu": "400m"}))
    time.sleep(0.5)
    pending = small_cluster.get("pods", "too-big")
    assert pending["status"]["phase"] == "Pending"
    assert pending["spec"].get("nodeName") is None


def test_unadvertised_extended_resource_stays_pending():
    instance = MiniCluster(start_api=False)
    instance.start()
    try:
        instance.apply(pod("gpu", requests={"nvidia.com/gpu": "1"}))
        time.sleep(0.5)
        current = instance.get("pods", "gpu")
        assert current["status"]["phase"] == "Pending"
        assert current["spec"].get("nodeName") is None
    finally:
        instance.stop()


def test_extended_resource_gpu_is_consumed():
    instance = MiniCluster(start_api=False, node_gpus=2)
    instance.start()
    try:
        for name in ("gpu-a", "gpu-b"):
            instance.apply(pod(name, requests={"nvidia.com/gpu": "1"}))
            instance.wait_for_pod(name)
        instance.apply(pod("gpu-c", requests={"nvidia.com/gpu": "1"}))
        time.sleep(0.5)
        assert instance.get("pods", "gpu-a")["spec"]["nodeName"] == "mini-node-1"
        assert instance.get("pods", "gpu-b")["spec"]["nodeName"] == "mini-node-1"
        assert instance.get("pods", "gpu-c")["status"]["phase"] == "Pending"
        assert instance.get("pods", "gpu-c")["spec"].get("nodeName") is None
    finally:
        instance.stop()


def test_init_container_requests_use_max_not_sum():
    instance = MiniCluster(start_api=False, node_cpu="500m")
    instance.start()
    try:
        body = pod("init-heavy")
        body["spec"]["initContainers"] = [{
            "name": "init", "image": "x",
            "resources": {"requests": {"cpu": "400m"}},
        }]
        body["spec"]["containers"][0]["resources"] = {"requests": {"cpu": "200m"}}
        instance.apply(body)
        instance.wait_for_pod("init-heavy")
        assert instance.get("pods", "init-heavy")["spec"]["nodeName"] == "mini-node-1"
    finally:
        instance.stop()


def test_node_selector(cluster):
    cluster.add_node("gpu-node", index=1, labels={"accelerator": "gpu"})
    time.sleep(0.4)
    cluster.apply(pod("gpu-pod", node_selector={"accelerator": "gpu"}))
    cluster.wait_for_pod("gpu-pod")
    assert cluster.get("pods", "gpu-pod")["spec"]["nodeName"] == "gpu-node"


def test_unschedulable_node(cluster):
    cluster.add_node("cordoned", index=1)
    time.sleep(0.4)
    node = cluster.get("nodes", "cordoned")
    node["spec"]["unschedulable"] = True
    cluster.store.update(("", "v1", "nodes"), node, kind="Node", namespace="")
    cluster.apply(pod("normal"))
    cluster.wait_for_pod("normal")
    assert cluster.get("pods", "normal")["spec"]["nodeName"] != "cordoned"


def test_taint_requires_toleration(cluster):
    cluster.add_node("tainted", index=1)
    time.sleep(0.4)
    node = cluster.get("nodes", "tainted")
    node["spec"]["taints"] = [{"key": "dedicated", "value": "db", "effect": "NoSchedule"}]
    cluster.store.update(("", "v1", "nodes"), node, kind="Node", namespace="")
    cluster.apply(pod("no-toleration"))
    cluster.apply(pod("toleration", tolerations=[
        {"key": "dedicated", "operator": "Equal", "value": "db", "effect": "NoSchedule"}]))
    cluster.wait_for_pod("no-toleration")
    cluster.wait_for_pod("toleration")
    assert cluster.get("pods", "no-toleration")["spec"]["nodeName"] != "tainted"
    assert cluster.get("pods", "toleration")["spec"]["nodeName"] == "tainted"


def test_node_affinity_required(cluster):
    cluster.add_node("zone-b", index=1, labels={"topology.kubernetes.io/zone": "mini-b"})
    time.sleep(0.4)
    affinity = {"nodeAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": {
        "nodeSelectorTerms": [{"matchExpressions": [
            {"key": "topology.kubernetes.io/zone", "operator": "In", "values": ["mini-b"]}]}]}}}
    cluster.apply(pod("zone-pod", affinity=affinity))
    cluster.wait_for_pod("zone-pod")
    assert cluster.get("pods", "zone-pod")["spec"]["nodeName"] == "zone-b"


def test_required_anti_affinity_respects_zone_domain():
    instance = MiniCluster(start_api=False, node_count=2)
    instance.start()
    try:
        first = pod("web-1")
        first["metadata"]["labels"] = {"app": "web"}
        first["spec"]["nodeSelector"] = {"kubernetes.io/hostname": "mini-node-1"}
        instance.apply(first)
        instance.wait_for_pod("web-1")

        second = pod("web-2", affinity={"podAntiAffinity": {
            "requiredDuringSchedulingIgnoredDuringExecution": [{
                "labelSelector": {"matchLabels": {"app": "web"}},
                "topologyKey": "topology.kubernetes.io/zone",
            }]}})
        second["metadata"]["labels"] = {"app": "web"}
        instance.apply(second)
        time.sleep(0.5)
        current = instance.get("pods", "web-2")
        assert current["status"]["phase"] == "Pending"
        assert current["spec"].get("nodeName") is None
    finally:
        instance.stop()


def test_interpod_anti_affinity_spreads(cluster):
    cluster.add_node("second", index=1)
    time.sleep(0.4)
    affinity = {"podAntiAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": [{
        "labelSelector": {"matchLabels": {"app": "spread"}},
        "topologyKey": "kubernetes.io/hostname"}]}}
    cluster.apply({
        "apiVersion": "apps/v1", "kind": "Deployment",
        "metadata": {"name": "spread", "namespace": "default"},
        "spec": {"replicas": 2, "selector": {"matchLabels": {"app": "spread"}},
                 "template": {"metadata": {"labels": {"app": "spread"}},
                              "spec": {"affinity": affinity,
                                       "containers": [{"name": "c", "image": "x"}]}}},
    })
    cluster.wait_until(lambda: len([p for p in cluster.list("pods") if p["metadata"]["name"].startswith("spread-")]) == 2
                       and all(p["spec"].get("nodeName") for p in cluster.list("pods") if p["metadata"]["name"].startswith("spread-")),
                       timeout=8, description="two scheduled pods")
    nodes = {p["spec"]["nodeName"] for p in cluster.list("pods") if p["metadata"]["name"].startswith("spread-")}
    assert nodes == {"mini-node-1", "second"}


def test_host_port_conflict(cluster):
    cluster.apply(pod("first", host_port=8080))
    cluster.wait_for_pod("first")
    cluster.apply(pod("second", host_port=8080))
    time.sleep(0.5)
    assert cluster.get("pods", "second")["spec"].get("nodeName") is None


def test_preemption_policy_never_does_not_evict(small_cluster):
    small_cluster.apply(pod("low", requests={"cpu": "400m"}))
    small_cluster.wait_for_pod("low")
    small_cluster.apply({"apiVersion": "scheduling.k8s.io/v1", "kind": "PriorityClass",
                         "metadata": {"name": "never"}, "value": 1000, "preemptionPolicy": "Never"})
    body = pod("high", requests={"cpu": "400m"})
    body["spec"]["priorityClassName"] = "never"
    # Deliberately omit spec.preemptionPolicy: it must be inherited from the
    # PriorityClass during defaulting.
    small_cluster.apply(body)
    time.sleep(0.5)
    assert small_cluster.get("pods", "low") is not None
    assert small_cluster.get("pods", "high")["status"]["phase"] == "Pending"


def test_preemption_evicts_lower_priority(small_cluster):
    small_cluster.apply({"apiVersion": "scheduling.k8s.io/v1", "kind": "PriorityClass",
                         "metadata": {"name": "high"}, "value": 100})
    small_cluster.apply(pod("low", requests={"cpu": "400m"}))
    small_cluster.wait_for_pod("low")
    small_cluster.apply({
        "apiVersion": "v1", "kind": "Pod",
        "metadata": {"name": "high-priority", "namespace": "default"},
        "spec": {"priorityClassName": "high",
                 "containers": [{"name": "c", "image": "x",
                                 "resources": {"requests": {"cpu": "300m"}}}]},
    })
    small_cluster.wait_for_pod("high-priority")
    small_cluster.wait_until(lambda: small_cluster.get("pods", "low") is None,
                             timeout=8, description="low priority preempted")
    assert small_cluster.get("pods", "high-priority")["status"]["phase"] == "Running"
