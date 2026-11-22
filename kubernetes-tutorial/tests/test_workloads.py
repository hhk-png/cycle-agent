from __future__ import annotations

import time
from datetime import datetime, timezone

from mini_k8s.controllers.workloads import CronJobController
from mini_k8s.store.store import ResourceStore


def test_deployment_reaches_ready(cluster, deploy):
    cluster.apply(deploy("web", replicas=3))
    deployment = cluster.wait_for_deployment("web")
    assert deployment["status"]["readyReplicas"] == 3
    assert deployment["status"]["availableReplicas"] == 3
    replicasets = [rs for rs in cluster.list("replicasets") if rs["metadata"]["name"].startswith("web-")]
    assert len(replicasets) == 1
    pods = [p for p in cluster.list("pods") if p["metadata"]["name"].startswith("web-")]
    assert len(pods) == 3
    owner = pods[0]["metadata"]["ownerReferences"][0]
    assert owner["kind"] == "ReplicaSet"


def test_deployment_rolling_update(cluster, deploy):
    cluster.apply(deploy("web", replicas=4))
    cluster.wait_for_deployment("web")
    updated = deploy("web", replicas=4, image="nginx:1.28")
    cluster.apply(updated)
    cluster.wait_until(
        lambda: (cluster.get("deployments", "web") or {}).get("status", {}).get("updatedReplicas") == 4
        and (cluster.get("deployments", "web") or {}).get("status", {}).get("availableReplicas") == 4
        and (cluster.get("deployments", "web") or {}).get("status", {}).get("replicas") == 4
        and (cluster.get("deployments", "web") or {}).get("status", {}).get("observedGeneration")
            == (cluster.get("deployments", "web") or {}).get("metadata", {}).get("generation"),
        timeout=15, description="rolling update complete")
    pods = [p for p in cluster.list("pods") if p["metadata"]["name"].startswith("web-")]
    assert all(p["spec"]["containers"][0]["image"] == "nginx:1.28" for p in pods)
    replicasets = [rs for rs in cluster.list("replicasets") if rs["metadata"]["name"].startswith("web-")]
    assert len(replicasets) == 2
    old = min(replicasets, key=lambda rs: int(rs["metadata"]["annotations"]["deployment.kubernetes.io/revision"]))
    assert old["spec"]["replicas"] == 0


def test_deployment_scale_down_removes_pods(cluster, deploy):
    cluster.apply(deploy("web", replicas=2))
    cluster.wait_for_deployment("web")
    body = cluster.get("deployments", "web")
    body["spec"]["replicas"] = 0
    cluster.apply(body)
    cluster.wait_until(lambda: (cluster.get("deployments", "web") or {}).get("status", {}).get("replicas") == 0,
                       timeout=8, description="deployment scaled down")
    assert len([p for p in cluster.list("pods") if p["metadata"]["name"].startswith("web-")]) == 0


def test_replicaset_adopts_orphan_pods(cluster, deploy):
    cluster.apply(deploy("web", replicas=1))
    cluster.wait_for_deployment("web")
    rs = [r for r in cluster.list("replicasets") if r["metadata"]["name"].startswith("web-")][0]
    # The Deployment's RS selector includes pod-template-hash, so the orphan
    # must carry exactly the selector labels to be adoptable.
    labels = dict(rs["spec"]["selector"]["matchLabels"])
    orphan = {
        "apiVersion": "v1", "kind": "Pod",
        "metadata": {"name": "orphan-pod", "namespace": "default", "labels": labels},
        "spec": {"containers": [{"name": "web", "image": "nginx:1.27",
                                 "resources": {"requests": {"cpu": "50m", "memory": "32Mi"}}}]},
    }
    cluster.store.create(("", "v1", "pods"), orphan, kind="Pod", namespace="default")
    original = [p for p in cluster.list("pods") if p["metadata"]["name"].startswith("web-")][0]
    cluster.delete(f"pod/{original['metadata']['name']}")
    cluster.wait_until(
        lambda: any(p["metadata"]["name"] == "orphan-pod" and p["metadata"].get("ownerReferences")
                    for p in cluster.list("pods")),
        timeout=20, description="orphan adoption")


def test_statefulset_creates_ordered_pods_and_pvcs(cluster):
    cluster.apply({
        "apiVersion": "apps/v1", "kind": "StatefulSet",
        "metadata": {"name": "db", "namespace": "default"},
        "spec": {
            "serviceName": "db", "replicas": 2,
            "selector": {"matchLabels": {"app": "db"}},
            "template": {"metadata": {"labels": {"app": "db"}},
                         "spec": {"containers": [{"name": "db", "image": "postgres:16",
                                                  "ports": [{"containerPort": 5432}],
                                                  "volumeMounts": [{"name": "data", "mountPath": "/data"}]}],
                                  "volumes": [{"name": "data",
                                               "persistentVolumeClaim": {"claimName": "data"}}]}},
            "volumeClaimTemplates": [{
                "metadata": {"name": "data"},
                "spec": {"accessModes": ["ReadWriteOnce"],
                         "resources": {"requests": {"storage": "1Gi"}}},
            }],
        },
    })
    cluster.wait_until(lambda: (cluster.get("statefulsets", "db") or {}).get("status", {}).get("readyReplicas") == 2,
                       timeout=10, description="statefulset ready")
    names = sorted(p["metadata"]["name"] for p in cluster.list("pods") if p["metadata"]["name"].startswith("db-"))
    assert names == ["db-0", "db-1"]
    pvcs = {p["metadata"]["name"]: p for p in cluster.list("persistentvolumeclaims")}
    assert set(pvcs) == {"data-db-0", "data-db-1"}
    assert all(pvc["status"]["phase"] == "Bound" for pvc in pvcs.values())
    assert cluster.get("pods", "db-0")["spec"]["volumes"][0]["persistentVolumeClaim"]["claimName"] == "data-db-0"


def test_statefulset_rolling_update_recreates_pods(cluster):
    body = {
        "apiVersion": "apps/v1", "kind": "StatefulSet",
        "metadata": {"name": "db", "namespace": "default"},
        "spec": {
            "serviceName": "db", "replicas": 2,
            "selector": {"matchLabels": {"app": "db"}},
            "template": {"metadata": {"labels": {"app": "db"}},
                         "spec": {"containers": [{"name": "db", "image": "postgres:15"}]}},
        },
    }
    cluster.apply(body)
    cluster.wait_until(lambda: (cluster.get("statefulsets", "db") or {}).get("status", {}).get("readyReplicas") == 2,
                       timeout=10, description="statefulset ready")
    body["spec"]["template"]["spec"]["containers"][0]["image"] = "postgres:16"
    cluster.apply(body)
    cluster.wait_until(
        lambda: (cluster.get("statefulsets", "db") or {}).get("status", {}).get("readyReplicas") == 2
        and all(p["spec"]["containers"][0]["image"] == "postgres:16"
                for p in cluster.list("pods") if p["metadata"]["name"].startswith("db-"))
        and len([p for p in cluster.list("pods") if p["metadata"]["name"].startswith("db-")]) == 2,
        timeout=15, description="statefulset update")


def test_statefulset_partition_reports_distinct_revisions(cluster):
    body = {
        "apiVersion": "apps/v1", "kind": "StatefulSet",
        "metadata": {"name": "db", "namespace": "default"},
        "spec": {
            "serviceName": "db", "replicas": 3,
            "selector": {"matchLabels": {"app": "db"}},
            "updateStrategy": {"type": "RollingUpdate", "rollingUpdate": {"partition": 1}},
            "template": {"metadata": {"labels": {"app": "db"}},
                         "spec": {"containers": [{"name": "db", "image": "postgres:15"}]}},
        },
    }
    cluster.apply(body)
    cluster.wait_until(lambda: (cluster.get("statefulsets", "db") or {}).get("status", {}).get("readyReplicas") == 3,
                       timeout=10, description="statefulset ready")
    body["spec"]["template"]["spec"]["containers"][0]["image"] = "postgres:16"
    cluster.apply(body)
    cluster.wait_until(
        lambda: (cluster.get("statefulsets", "db") or {}).get("status", {}).get("updatedReplicas") == 2
        and (cluster.get("statefulsets", "db") or {}).get("status", {}).get("readyReplicas") == 3,
        timeout=15, description="partitioned update")
    status = cluster.get("statefulsets", "db")["status"]
    assert status["currentRevision"] != status["updateRevision"]
    assert status["currentReplicas"] == 1
    assert status["updatedReplicas"] == 2


def test_daemonset_one_pod_per_node(cluster):
    cluster.add_node("worker-b", index=1)
    time.sleep(0.3)
    cluster.apply({
        "apiVersion": "apps/v1", "kind": "DaemonSet",
        "metadata": {"name": "agent", "namespace": "kube-system"},
        "spec": {"selector": {"matchLabels": {"app": "agent"}},
                 "template": {"metadata": {"labels": {"app": "agent"}},
                              "spec": {"containers": [{"name": "agent", "image": "agent:1"}]}}},
    })
    cluster.wait_until(lambda: (cluster.get("daemonsets", "agent", "kube-system") or {}).get("status", {}).get("numberReady") == 2,
                       timeout=10, description="daemonset ready")
    pods = [p for p in cluster.list("pods", "kube-system") if p["metadata"]["name"].startswith("agent-")]
    nodes = {p["spec"].get("nodeName") for p in pods}
    assert nodes == {"mini-node-1", "worker-b"}


def test_job_completes(cluster):
    cluster.apply({
        "apiVersion": "batch/v1", "kind": "Job",
        "metadata": {"name": "work", "namespace": "default"},
        "spec": {
            "completions": 3, "parallelism": 2,
            "template": {"metadata": {"labels": {"job": "work"}},
                         "spec": {"restartPolicy": "Never",
                                  "containers": [{"name": "w", "image": "busybox",
                                                  "command": ["echo", "done"]}]}},
        },
    })
    cluster.wait_until(lambda: (cluster.get("jobs", "work") or {}).get("status", {}).get("succeeded") == 3,
                       timeout=10, description="job completion")
    job = cluster.get("jobs", "work")
    assert any(c["type"] == "Complete" and c["status"] == "True" for c in job["status"]["conditions"])


def test_job_retries_failed_pods(cluster):
    cluster.apply({
        "apiVersion": "batch/v1", "kind": "Job",
        "metadata": {"name": "retry", "namespace": "default"},
        "spec": {
            "completions": 2, "parallelism": 1, "backoffLimit": 5,
            "template": {"metadata": {"annotations": {
                            "mini-k8s.io/simulate.exit-code": "1",
                            "mini-k8s.io/simulate.run-ticks": "1"}},
                         "spec": {"restartPolicy": "Never",
                                  "containers": [{"name": "w", "image": "busybox"}]}},
        },
    })
    cluster.wait_until(lambda: (cluster.get("jobs", "retry") or {}).get("status", {}).get("failed", 0) > 0,
                       timeout=8, description="job failure recorded")
    assert (cluster.get("jobs", "retry") or {}).get("status", {}).get("failed") > 0


def test_cronjob_schedule_with_fake_clock():
    store = ResourceStore()
    store.create(("batch", "v1", "cronjobs"), {
        "apiVersion": "batch/v1", "kind": "CronJob",
        "metadata": {"name": "c", "namespace": "default"},
        "spec": {"schedule": "*/5 * * * *",
                 "jobTemplate": {"spec": {"template": {"spec": {
                     "containers": [{"name": "c", "image": "x"}], "restartPolicy": "Never"}}}}},
    }, kind="CronJob", namespace="default")
    now = [datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)]
    controller = CronJobController(store, time_fn=lambda: now[0])
    controller.reconcile()
    assert store.list(("batch", "v1", "jobs"))[0] == []
    now[0] = datetime(2026, 1, 1, 0, 6, tzinfo=timezone.utc)
    controller.reconcile()
    jobs = store.list(("batch", "v1", "jobs"))[0]
    assert len(jobs) == 1
    assert jobs[0]["metadata"]["labels"]["batch.kubernetes.io/cronjob"] == "c"
    now[0] = datetime(2026, 1, 1, 0, 7, tzinfo=timezone.utc)
    controller.reconcile()
    assert len(store.list(("batch", "v1", "jobs"))[0]) == 1


def test_hpa_scales_deployment(cluster):
    cluster.apply({
        "apiVersion": "apps/v1", "kind": "Deployment",
        "metadata": {"name": "cpu-app", "namespace": "default"},
        "spec": {"replicas": 2, "selector": {"matchLabels": {"app": "cpu"}},
                 "template": {"metadata": {"labels": {"app": "cpu"},
                                           "annotations": {"mini-k8s.io/simulate.cpu-usage": "100m"}},
                              "spec": {"containers": [{"name": "app", "image": "x",
                                                       "resources": {"requests": {"cpu": "100m"}}}]}}},
    })
    cluster.apply({
        "apiVersion": "autoscaling/v2", "kind": "HorizontalPodAutoscaler",
        "metadata": {"name": "cpu-app", "namespace": "default"},
        "spec": {"scaleTargetRef": {"apiVersion": "apps/v1", "kind": "Deployment", "name": "cpu-app"},
                 "minReplicas": 1, "maxReplicas": 6,
                 "metrics": [{"type": "Resource", "resource": {
                     "name": "cpu", "target": {"type": "Utilization", "averageUtilization": 50}}}]},
    })
    cluster.wait_until(lambda: (cluster.get("deployments", "cpu-app") or {}).get("spec", {}).get("replicas") == 4,
                       timeout=8, description="HPA scale to 4")
    hpa = cluster.get("horizontalpodautoscalers", "cpu-app")
    assert hpa["status"]["desiredReplicas"] == 4
    metric = hpa["status"]["currentMetrics"][0]["resource"]["current"]["averageUtilization"]
    assert metric == 100


def test_hpa_scales_deployment_on_memory(cluster):
    cluster.apply({
        "apiVersion": "apps/v1", "kind": "Deployment",
        "metadata": {"name": "mem-app", "namespace": "default"},
        "spec": {"replicas": 2, "selector": {"matchLabels": {"app": "mem"}},
                 "template": {"metadata": {"labels": {"app": "mem"},
                                           "annotations": {"mini-k8s.io/simulate.memory-usage": "200Mi"}},
                              "spec": {"containers": [{"name": "app", "image": "x",
                                                       "resources": {"requests": {"memory": "100Mi"}}}]}}},
    })
    cluster.apply({
        "apiVersion": "autoscaling/v2", "kind": "HorizontalPodAutoscaler",
        "metadata": {"name": "mem-app", "namespace": "default"},
        "spec": {"scaleTargetRef": {"apiVersion": "apps/v1", "kind": "Deployment", "name": "mem-app"},
                 "minReplicas": 1, "maxReplicas": 6,
                 "metrics": [{"type": "Resource", "resource": {
                     "name": "memory", "target": {"type": "Utilization", "averageUtilization": 100}}}]},
    })
    cluster.wait_until(lambda: (cluster.get("deployments", "mem-app") or {}).get("spec", {}).get("replicas") == 4,
                       timeout=8, description="HPA scale to 4 on memory")
    hpa = cluster.get("horizontalpodautoscalers", "mem-app")
    assert hpa["status"]["currentMetrics"][0]["resource"]["current"]["averageUtilization"] == 200
    assert hpa["status"]["currentMetrics"][0]["resource"]["current"]["averageValue"] == "200Mi"


def test_hpa_respects_max_replicas(cluster):
    cluster.apply({
        "apiVersion": "apps/v1", "kind": "Deployment",
        "metadata": {"name": "hot", "namespace": "default"},
        "spec": {"replicas": 1, "selector": {"matchLabels": {"app": "hot"}},
                 "template": {"metadata": {"labels": {"app": "hot"},
                                           "annotations": {"mini-k8s.io/simulate.cpu-usage": "1000m"}},
                              "spec": {"containers": [{"name": "app", "image": "x",
                                                       "resources": {"requests": {"cpu": "100m"}}}]}}},
    })
    cluster.apply({
        "apiVersion": "autoscaling/v2", "kind": "HorizontalPodAutoscaler",
        "metadata": {"name": "hot", "namespace": "default"},
        "spec": {"scaleTargetRef": {"apiVersion": "apps/v1", "kind": "Deployment", "name": "hot"},
                 "minReplicas": 1, "maxReplicas": 2},
    })
    cluster.wait_until(lambda: (cluster.get("deployments", "hot") or {}).get("spec", {}).get("replicas") == 2,
                       timeout=8, description="HPA capped at max")
    assert (cluster.get("deployments", "hot") or {}).get("spec", {}).get("replicas") == 2


def test_indexed_job_reuses_failed_index(cluster):
    cluster.apply({
        "apiVersion": "batch/v1", "kind": "Job",
        "metadata": {"name": "indexed", "namespace": "default"},
        "spec": {"completions": 3, "parallelism": 3, "completionMode": "Indexed",
                 "template": {"metadata": {"labels": {"job": "indexed"}},
                              "spec": {"restartPolicy": "Never",
                                       "containers": [{"name": "c", "image": "busybox"}]}}},
    })
    def active():
        return [p for p in cluster.list("pods")
                if p["metadata"]["name"].startswith("indexed-")
                and p.get("status", {}).get("phase") not in ("Succeeded", "Failed")]
    cluster.wait_until(lambda: len(active()) == 3, timeout=8, description="three indexed pods")
    victim = next(p for p in active()
                  if p["metadata"]["labels"]["batch.kubernetes.io/job-completion-index"] == "0")
    status = victim.get("status", {})
    status["phase"] = "Failed"
    cluster.store.update_status(("", "v1", "pods"), "default", victim["metadata"]["name"], status, kind="Pod")
    cluster.wait_until(lambda: len(active()) == 3, timeout=8, description="replacement pod")
    indexes = {p["metadata"]["labels"]["batch.kubernetes.io/job-completion-index"] for p in active()}
    assert indexes == {"0", "1", "2"}


def test_daemonset_respects_node_taints(cluster):
    cluster.add_node("worker-b", index=1,
                     taints=[{"key": "dedicated", "value": "gpu", "effect": "NoSchedule"}])
    time.sleep(0.3)
    body = {
        "apiVersion": "apps/v1", "kind": "DaemonSet",
        "metadata": {"name": "agent", "namespace": "kube-system"},
        "spec": {"selector": {"matchLabels": {"app": "agent"}},
                 "template": {"metadata": {"labels": {"app": "agent"}},
                              "spec": {"containers": [{"name": "agent", "image": "agent:1"}]}}},
    }
    cluster.apply(body)
    cluster.wait_until(lambda: (cluster.get("daemonsets", "agent", "kube-system") or {}).get("status", {}).get("numberReady") == 1,
                       timeout=10, description="daemonset skips tainted node")
    nodes = {p["spec"]["nodeName"] for p in cluster.list("pods", "kube-system")
             if p["metadata"]["name"].startswith("agent-")}
    assert nodes == {"mini-node-1"}

    body["spec"]["template"]["spec"]["tolerations"] = [{"operator": "Exists"}]
    cluster.apply(body)
    cluster.wait_until(lambda: (cluster.get("daemonsets", "agent", "kube-system") or {}).get("status", {}).get("numberReady") == 2,
                       timeout=10, description="daemonset tolerates taint")
