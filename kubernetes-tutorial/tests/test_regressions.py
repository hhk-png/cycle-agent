"""Regression tests for controller/scheduler semantics found by auditing.

Each test encodes a behaviour that a naive implementation gets subtly wrong.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

import pytest

from mini_k8s.cluster import MiniCluster
from mini_k8s.controllers.autoscaling import MetricsController
from mini_k8s.controllers.workloads import CronJobController
from mini_k8s.errors import ApiError
from mini_k8s.kubelet.kubelet import _qos_class
from mini_k8s.store.store import ResourceStore


def pod(name: str, *, cpu: str = "100m", priority: int = 0, labels: dict | None = None,
        node: str | None = None, ready: bool = True, ip: str = "10.0.0.10") -> dict:
    body = {
        "apiVersion": "v1", "kind": "Pod",
        "metadata": {"name": name, "namespace": "default", "labels": labels or {"app": name}},
        "spec": {
            "priority": priority,
            "containers": [{"name": "c", "image": "demo:v1",
                            "resources": {"requests": {"cpu": cpu, "memory": "32Mi"}}}],
        },
        "status": {
            "phase": "Running", "podIP": ip,
            "conditions": [{"type": "Ready", "status": "True" if ready else "False"}],
        },
    }
    if node:
        body["spec"]["nodeName"] = node
    return body


def test_replicaset_scale_down_prefers_unscheduled_pod(cluster, deploy):
    cluster.apply(deploy("web", replicas=2))
    cluster.wait_for_deployment("web")
    cluster.scheduler.stop()
    pods = [p for p in cluster.list("pods") if p["metadata"]["name"].startswith("web-")]
    assert len(pods) == 2
    # Unbind one pod.  The scheduler is stopped, so it stays unscheduled.
    pending = pods[0]
    pending["spec"].pop("nodeName", None)
    cluster.store.update(("", "v1", "pods"), pending, kind="Pod", namespace="default")
    scheduled_name = pods[1]["metadata"]["name"]

    body = cluster.get("deployments", "web")
    body["spec"]["replicas"] = 1
    cluster.apply(body)
    cluster.wait_until(
        lambda: len([p for p in cluster.list("pods") if p["metadata"]["name"].startswith("web-")]) == 1,
        timeout=10, description="scale down to one pod",
    )
    remaining = [p for p in cluster.list("pods") if p["metadata"]["name"].startswith("web-")][0]
    assert remaining["metadata"]["name"] == scheduled_name
    assert remaining["spec"].get("nodeName")


def test_recreate_waits_for_old_pods_to_disappear(cluster, deploy):
    manifest = deploy("web", replicas=2)
    manifest["spec"]["strategy"] = {"type": "Recreate"}
    cluster.apply(manifest)
    cluster.wait_for_deployment("web")
    old_pods = [p for p in cluster.list("pods") if p["metadata"]["name"].startswith("web-")]
    for old in old_pods:
        old["metadata"]["finalizers"] = ["test/hold"]
        cluster.store.update(("", "v1", "pods"), old, kind="Pod", namespace="default")

    updated = deploy("web", replicas=2, image="nginx:1.28")
    updated["spec"]["strategy"] = {"type": "Recreate"}
    cluster.apply(updated)
    time.sleep(0.5)
    # The old pods are terminating (finalizer) and must block the new ones.
    pods = [p for p in cluster.list("pods") if p["metadata"]["name"].startswith("web-")]
    assert all(p["spec"]["containers"][0]["image"] == "nginx:1.27" for p in pods)

    for old in pods:
        current = cluster.get("pods", old["metadata"]["name"])
        current["metadata"]["finalizers"] = []
        cluster.store.update(("", "v1", "pods"), current, kind="Pod", namespace="default")
    cluster.wait_until(
        lambda: all(p["spec"]["containers"][0]["image"] == "nginx:1.28"
                    for p in cluster.list("pods") if p["metadata"]["name"].startswith("web-"))
        and len([p for p in cluster.list("pods") if p["metadata"]["name"].startswith("web-")]) == 2,
        timeout=15, description="recreate completed",
    )


def test_preemption_deletes_minimal_prefix():
    instance = MiniCluster(node_cpu="500m", node_memory="4Gi", start_api=False)
    instance.start()
    try:
        for index in range(3):
            instance.apply(pod(f"low-{index}", cpu="100m", priority=1))
        instance.wait_until(
            lambda: len([p for p in instance.list("pods") if p["metadata"]["name"].startswith("low-")
                         and p.get("spec", {}).get("nodeName")]) == 3,
            timeout=10, description="three low priority pods scheduled",
        )
        instance.apply(pod("high", cpu="250m", priority=10))
        instance.wait_until(
            lambda: (instance.get("pods", "high") or {}).get("spec", {}).get("nodeName"),
            timeout=10, description="high priority pod scheduled after preemption",
        )
        lows = [p for p in instance.list("pods") if p["metadata"]["name"].startswith("low-")]
        # 300m used, 250m requested, 500m capacity: freeing one 100m pod is
        # enough.  A naive implementation evicts all lower-priority pods.
        assert len(lows) == 2
    finally:
        instance.stop()


def test_pdb_eviction_spends_budget_even_without_controller(api_cluster):
    for index in range(3):
        api_cluster.apply(pod(f"guarded-{index}", cpu="50m", labels={"app": "guarded"}))
    api_cluster.wait_until(
        lambda: len([p for p in api_cluster.list("pods") if p["metadata"]["name"].startswith("guarded-")
                     and any(c.get("type") == "Ready" and c.get("status") == "True"
                             for c in p.get("status", {}).get("conditions", []))]) == 3,
        timeout=10, description="guarded pods ready",
    )
    api_cluster.apply({
        "apiVersion": "policy/v1", "kind": "PodDisruptionBudget",
        "metadata": {"name": "guard", "namespace": "default"},
        "spec": {"minAvailable": 2, "selector": {"matchLabels": {"app": "guarded"}}},
    })
    api_cluster.wait_until(
        lambda: (api_cluster.get("poddisruptionbudgets", "guard") or {}).get("status", {})
        .get("disruptionsAllowed") == 1,
        timeout=5, description="pdb status",
    )
    controller = next(c for c in api_cluster.controllers if c.name == "disruption-controller")
    controller.stop()

    api_cluster.api.evict("guarded-0")
    pdb = api_cluster.get("poddisruptionbudgets", "guard")
    assert pdb["status"]["disruptionsAllowed"] == 0
    assert "guarded-0" in pdb["status"]["disruptedPods"]
    with pytest.raises(ApiError) as exc:
        api_cluster.api.evict("guarded-1")
    assert exc.value.status == 429


def test_scheduler_honours_scheduler_name_and_reports_unschedulable(cluster):
    cluster.apply({
        "apiVersion": "v1", "kind": "Pod",
        "metadata": {"name": "foreign", "namespace": "default"},
        "spec": {"schedulerName": "another-scheduler",
                 "containers": [{"name": "c", "image": "demo:v1"}]},
    })
    time.sleep(0.4)
    assert "nodeName" not in cluster.get("pods", "foreign")["spec"]

    cluster.apply({
        "apiVersion": "v1", "kind": "Pod",
        "metadata": {"name": "too-big", "namespace": "default"},
        "spec": {"containers": [{"name": "c", "image": "demo:v1",
                                 "resources": {"requests": {"cpu": "999"}}}]},
    })
    cluster.wait_until(
        lambda: any(c.get("type") == "PodScheduled" and c.get("status") == "False"
                    and c.get("reason") == "Unschedulable"
                    for c in (cluster.get("pods", "too-big") or {}).get("status", {}).get("conditions", [])),
        timeout=5, description="unschedulable condition",
    )


def test_qos_considers_init_containers():
    guaranteed = {"requests": {"cpu": "1", "memory": "1Gi"},
                  "limits": {"cpu": "1", "memory": "1Gi"}}
    spec = {
        "containers": [{"resources": guaranteed}],
        "initContainers": [{"resources": guaranteed}],
    }
    assert _qos_class(spec) == "Guaranteed"
    # An init container without requests/limits makes the Pod Burstable, even
    # when the app container is Guaranteed.
    spec["initContainers"] = [{}]
    assert _qos_class(spec) == "Burstable"


def test_create_ignores_client_deletion_timestamp(api_cluster):
    body = {
        "apiVersion": "v1", "kind": "ConfigMap",
        "metadata": {"name": "sneaky", "namespace": "default",
                     "deletionTimestamp": "2020-01-01T00:00:00Z",
                     "deletionGracePeriodSeconds": 0},
        "data": {"k": "v"},
    }
    created = api_cluster.api.create(body)
    assert "deletionTimestamp" not in created["metadata"]
    assert "deletionGracePeriodSeconds" not in created["metadata"]


def test_external_name_service_resolves_to_target(cluster):
    cluster.apply({
        "apiVersion": "v1", "kind": "Service",
        "metadata": {"name": "external", "namespace": "default"},
        "spec": {"type": "ExternalName", "externalName": "db.example.com"},
    })
    assert cluster.dns.resolve("external") == ["db.example.com"]


def test_update_cannot_inject_deletion_timestamp(api_cluster):
    body = {
        "apiVersion": "v1", "kind": "ConfigMap",
        "metadata": {"name": "healthy", "namespace": "default"},
        "data": {"k": "v"},
    }
    created = api_cluster.api.create(body)
    created["metadata"]["deletionTimestamp"] = "2020-01-01T00:00:00Z"
    created["metadata"]["deletionGracePeriodSeconds"] = 0
    updated = api_cluster.api.replace(created)
    assert "deletionTimestamp" not in updated["metadata"]
    assert "deletionGracePeriodSeconds" not in updated["metadata"]
    assert api_cluster.get("configmaps", "healthy") is not None

    # PATCH is the other write path that must strip client-supplied deletion state.
    patched = api_cluster.api.patch("v1", "ConfigMap", "healthy", {
        "metadata": {"deletionTimestamp": "2020-01-01T00:00:00Z"}}, "default")
    assert "deletionTimestamp" not in patched["metadata"]
    assert api_cluster.get("configmaps", "healthy") is not None


def test_limit_without_request_defaults_request_and_qos(api_cluster):
    created = api_cluster.api.create({
        "apiVersion": "v1", "kind": "Pod",
        "metadata": {"name": "limits-only", "namespace": "default"},
        "spec": {"containers": [{
            "name": "app", "image": "demo:v1",
            "resources": {"limits": {"cpu": "250m", "memory": "128Mi"}},
        }]},
    })
    resources = created["spec"]["containers"][0]["resources"]
    assert resources["requests"] == {"cpu": "250m", "memory": "128Mi"}
    api_cluster.wait_until(
        lambda: (api_cluster.get("pods", "limits-only") or {}).get("status", {}).get("qosClass") == "Guaranteed",
        timeout=5, description="limits-only Pod is Guaranteed",
    )


def test_extended_resources_are_integral_and_symmetric(api_cluster):
    def gpu_pod(name: str, requests: dict, limits: dict | None = None) -> dict:
        resources = {"requests": requests}
        if limits is not None:
            resources["limits"] = limits
        return {
            "apiVersion": "v1", "kind": "Pod",
            "metadata": {"name": name, "namespace": "default"},
            "spec": {"containers": [{"name": "app", "image": "demo:v1", "resources": resources}]},
        }

    created = api_cluster.api.create(gpu_pod("gpu-limit-only", {}, {"nvidia.com/gpu": "1"}))
    assert created["spec"]["containers"][0]["resources"]["requests"]["nvidia.com/gpu"] == "1"

    with pytest.raises(ApiError):
        api_cluster.api.create(gpu_pod("gpu-fractional", {"nvidia.com/gpu": "0.5"},
                                       {"nvidia.com/gpu": "0.5"}))
    with pytest.raises(ApiError):
        api_cluster.api.create(gpu_pod("gpu-mismatch", {"nvidia.com/gpu": "1"},
                                       {"nvidia.com/gpu": "2"}))


def test_pod_metrics_split_multicontainer_usage(cluster):
    store = ResourceStore()
    store.create(("", "v1", "pods"), {
        "apiVersion": "v1", "kind": "Pod",
        "metadata": {"name": "two", "namespace": "default",
                     "annotations": {"mini-k8s.io/simulate.cpu-usage": "240m",
                                     "mini-k8s.io/simulate.memory-usage": "300Mi"}},
        "spec": {"nodeName": "mini-node-1", "containers": [
            {"name": "a", "image": "x", "resources": {"requests": {"cpu": "100m", "memory": "100Mi"}}},
            {"name": "b", "image": "x", "resources": {"requests": {"cpu": "100m", "memory": "200Mi"}}},
        ]},
        "status": {"phase": "Running"},
    }, kind="Pod", namespace="default")
    controller = MetricsController(store)
    controller.reconcile()
    metrics = store.get(("metrics.k8s.io", "v1beta1", "pods"), "default", "two")
    usage = metrics["containers"]
    assert sum(int(item["usage"]["cpu"][:-1]) for item in usage) == 240
    assert sum(int(item["usage"]["memory"]) for item in usage) == 300 * 1024 * 1024
    # CPU requests are equal (100m each) -> 120m/120m.  Memory requests are
    # 100Mi:200Mi -> 100Mi/200Mi.
    assert usage[0]["usage"] == {"cpu": "120m", "memory": str(100 * 1024 * 1024)}
    assert usage[1]["usage"] == {"cpu": "120m", "memory": str(200 * 1024 * 1024)}


def test_failed_job_gets_completion_time_and_ttl_deletes_it(cluster):
    cluster.apply({
        "apiVersion": "batch/v1", "kind": "Job",
        "metadata": {"name": "failfast", "namespace": "default"},
        "spec": {
            "backoffLimit": 0, "ttlSecondsAfterFinished": 0,
            "template": {
                "metadata": {"annotations": {"mini-k8s.io/simulate.exit-code": "1"}},
                "spec": {"restartPolicy": "Never", "containers": [{"name": "c", "image": "x"}]},
            },
        },
    })
    cluster.wait_until(lambda: cluster.get("jobs", "failfast") is None,
                       timeout=10, description="failed Job TTL cleanup")


def test_deletecollection_honours_selector(api_cluster):
    for name, team in (("keep", "a"), ("drop-1", "b"), ("drop-2", "b")):
        api_cluster.api.create({
            "apiVersion": "v1", "kind": "ConfigMap",
            "metadata": {"name": name, "namespace": "default", "labels": {"team": team}},
            "data": {"k": "v"},
        })
    path = api_cluster.api.path_for("v1", "ConfigMap", "default")
    result = api_cluster.api.request("DELETE", path, {"apiVersion": "v1", "kind": "DeleteOptions"},
                                     params={"labelSelector": "team=b"})
    assert result["status"] == "Success"
    assert result["deleted"] == 2
    remaining = [item["metadata"]["name"] for item in api_cluster.list("configmaps")]
    assert remaining == ["keep"]


def test_cronjob_active_tracks_all_running_jobs():
    store = ResourceStore()
    store.create(("batch", "v1", "cronjobs"), {
        "apiVersion": "batch/v1", "kind": "CronJob",
        "metadata": {"name": "c", "namespace": "default"},
        "spec": {"schedule": "*/5 * * * *", "concurrencyPolicy": "Allow",
                 "jobTemplate": {"spec": {"template": {"spec": {
                     "containers": [{"name": "c", "image": "x"}], "restartPolicy": "Never"}}}}},
    }, kind="CronJob", namespace="default")
    now = [datetime(2026, 1, 1, 0, 6, tzinfo=timezone.utc)]
    controller = CronJobController(store, time_fn=lambda: now[0])
    controller.reconcile()  # initializes lastScheduleTime, does not fire
    now[0] = datetime(2026, 1, 1, 0, 11, tzinfo=timezone.utc)
    controller.reconcile()
    now[0] = datetime(2026, 1, 1, 0, 16, tzinfo=timezone.utc)
    controller.reconcile()
    status = store.get(("batch", "v1", "cronjobs"), "default", "c")["status"]
    assert len(status["active"]) == 2
    assert len(store.list(("batch", "v1", "jobs"))[0]) == 2

    # Completed Jobs must leave status.active without waiting for the next
    # schedule.  Reconcile at the same minute to exercise the early-return path.
    for job in store.list(("batch", "v1", "jobs"))[0]:
        store.update_status(("batch", "v1", "jobs"), "default", job["metadata"]["name"],
                            {"conditions": [{"type": "Complete", "status": "True"}]}, kind="Job")
    controller.reconcile()
    status = store.get(("batch", "v1", "cronjobs"), "default", "c")["status"]
    assert status["active"] == []


def test_session_affinity_is_stable(cluster, deploy):
    cluster.apply(deploy("web", replicas=2))
    cluster.wait_for_deployment("web")
    cluster.apply({
        "apiVersion": "v1", "kind": "Service",
        "metadata": {"name": "web", "namespace": "default"},
        "spec": {
            "selector": {"app": "web"},
            "ports": [{"name": "http", "port": 80, "targetPort": 80}],
            "sessionAffinity": "ClientIP",
            "sessionAffinityConfig": {"clientIP": {"timeoutSeconds": 10800}},
        },
    })
    cluster.wait_until(
        lambda: len((cluster.get("endpoints", "web") or {}).get("subsets", [{}])[0].get("addresses", [])) == 2,
        timeout=8, description="two endpoints",
    )
    source = {"metadata": {"name": "client"}, "status": {"podIP": "10.244.1.50"}}
    first = cluster.proxy.connect(source, "web", "default", 80)
    second = cluster.proxy.connect(source, "web", "default", 80)
    assert first["connected"] and second["connected"]
    assert first["endpoint"] == second["endpoint"]


def test_daemonset_stays_on_cordoned_node_and_honours_affinity(cluster):
    cluster.add_node("worker-b", index=1, labels={"dedicated": "batch"})
    cluster.apply({
        "apiVersion": "apps/v1", "kind": "DaemonSet",
        "metadata": {"name": "agent", "namespace": "kube-system"},
        "spec": {
            "selector": {"matchLabels": {"app": "agent"}},
            "template": {
                "metadata": {"labels": {"app": "agent"}},
                "spec": {
                    "nodeSelector": {"kubernetes.io/os": "linux"},
                    "affinity": {"nodeAffinity": {
                        "requiredDuringSchedulingIgnoredDuringExecution": {
                            "nodeSelectorTerms": [{"matchExpressions": [
                                {"key": "dedicated", "operator": "In", "values": ["batch"]}
                            ]}]
                        }
                    }},
                    "containers": [{"name": "agent", "image": "agent:v1"}],
                },
            },
        },
    })
    cluster.wait_until(
        lambda: (cluster.get("daemonsets", "agent", "kube-system") or {}).get("status", {}).get("numberReady") == 1,
        timeout=8, description="daemonset on affinity node",
    )
    pods = [p for p in cluster.list("pods", "kube-system") if p["metadata"]["name"].startswith("agent-")]
    assert [p["spec"]["nodeName"] for p in pods] == ["worker-b"]

    node = cluster.get("nodes", "worker-b")
    node["spec"]["unschedulable"] = True
    cluster.store.update(("", "v1", "nodes"), node, kind="Node", namespace="")
    time.sleep(0.4)
    pods = [p for p in cluster.list("pods", "kube-system") if p["metadata"]["name"].startswith("agent-")]
    assert len(pods) == 1 and not pods[0]["metadata"].get("deletionTimestamp")


def test_hpa_tolerance_avoids_flapping(cluster):
    cluster.apply({
        "apiVersion": "apps/v1", "kind": "Deployment",
        "metadata": {"name": "tol", "namespace": "default"},
        "spec": {"replicas": 4, "selector": {"matchLabels": {"app": "tol"}},
                 "template": {"metadata": {"labels": {"app": "tol"},
                                           "annotations": {"mini-k8s.io/simulate.cpu-usage": "52.5m"}},
                              "spec": {"containers": [{"name": "app", "image": "x",
                                                       "resources": {"requests": {"cpu": "100m"}}}]}}},
    })
    cluster.apply({
        "apiVersion": "autoscaling/v2", "kind": "HorizontalPodAutoscaler",
        "metadata": {"name": "tol", "namespace": "default"},
        "spec": {"scaleTargetRef": {"apiVersion": "apps/v1", "kind": "Deployment", "name": "tol"},
                 "minReplicas": 1, "maxReplicas": 10,
                 "metrics": [{"type": "Resource", "resource": {
                     "name": "cpu", "target": {"type": "Utilization", "averageUtilization": 50}}}]},
    })
    cluster.wait_for_deployment("tol")
    time.sleep(0.6)
    # 52.5/50 = 1.05, inside the default 10% tolerance: do not scale.
    assert cluster.get("deployments", "tol")["spec"]["replicas"] == 4


def test_hpa_takes_max_across_metrics(cluster):
    cluster.apply({
        "apiVersion": "apps/v1", "kind": "Deployment",
        "metadata": {"name": "multi", "namespace": "default"},
        "spec": {"replicas": 2, "selector": {"matchLabels": {"app": "multi"}},
                 "template": {"metadata": {"labels": {"app": "multi"},
                                           "annotations": {
                                               "mini-k8s.io/simulate.cpu-usage": "70m",
                                               "mini-k8s.io/simulate.memory-usage": "200Mi"}},
                              "spec": {"containers": [{"name": "app", "image": "x",
                                                       "resources": {"requests": {
                                                           "cpu": "100m", "memory": "100Mi"}}}]}}},
    })
    cluster.apply({
        "apiVersion": "autoscaling/v2", "kind": "HorizontalPodAutoscaler",
        "metadata": {"name": "multi", "namespace": "default"},
        "spec": {"scaleTargetRef": {"apiVersion": "apps/v1", "kind": "Deployment", "name": "multi"},
                 "minReplicas": 1, "maxReplicas": 10,
                 "metrics": [
                     {"type": "Resource", "resource": {
                         "name": "cpu", "target": {"type": "Utilization", "averageUtilization": 50}}},
                     {"type": "Resource", "resource": {
                         "name": "memory", "target": {"type": "Utilization", "averageUtilization": 100}}},
                 ]},
    })
    # CPU: ceil(2 * 1.4) = 3; memory: ceil(2 * 2.0) = 4.  HPA takes the max.
    cluster.wait_until(
        lambda: (cluster.get("deployments", "multi") or {}).get("spec", {}).get("replicas") == 4,
        timeout=8, description="HPA max across metrics",
    )


def test_endpoints_include_not_ready_and_slice_zone_hint(cluster):
    # Keep the hand-written statuses from being overwritten by a kubelet.
    for kubelet in cluster.kubelets.values():
        kubelet.stop()
    cluster.apply({
        "apiVersion": "v1", "kind": "Service",
        "metadata": {"name": "ep", "namespace": "default"},
        "spec": {"selector": {"app": "ep"}, "ports": [{"name": "http", "port": 80, "targetPort": 8080}]},
    })
    ready = pod("ep-ready", labels={"app": "ep"}, node="mini-node-1", ip="10.244.0.11")
    unready = pod("ep-unready", labels={"app": "ep"}, node="mini-node-1", ip="10.244.0.12", ready=False)
    cluster.store.create(("", "v1", "pods"), ready, kind="Pod", namespace="default")
    cluster.store.create(("", "v1", "pods"), unready, kind="Pod", namespace="default")
    cluster.wait_until(
        lambda: (cluster.get("endpoints", "ep") or {}).get("subsets", [{}])[0].get("notReadyAddresses"),
        timeout=5, description="notReadyAddresses",
    )
    subset = cluster.get("endpoints", "ep")["subsets"][0]
    assert [a["ip"] for a in subset["addresses"]] == ["10.244.0.11"]
    assert [a["ip"] for a in subset["notReadyAddresses"]] == ["10.244.0.12"]

    service = cluster.get("services", "ep")
    service["spec"]["publishNotReadyAddresses"] = True
    cluster.apply(service)
    cluster.wait_until(
        lambda: len((cluster.get("endpoints", "ep") or {}).get("subsets", [{}])[0].get("addresses", [])) == 2,
        timeout=5, description="publishNotReadyAddresses",
    )
    subset = cluster.get("endpoints", "ep")["subsets"][0]
    assert not subset.get("notReadyAddresses")

    slices = [s for s in cluster.list("endpointslices")
              if s["metadata"].get("labels", {}).get("kubernetes.io/service-name") == "ep"]
    assert len(slices) == 1
    assert slices[0].get("hints", {}).get("forZones") == [{"name": "mini-a"}]
