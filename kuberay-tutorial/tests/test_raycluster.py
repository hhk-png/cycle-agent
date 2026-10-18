from __future__ import annotations

import copy

from minikuberay import constants as C
from minikuberay.models.core import pod_is_ready
from tests.conftest import Harness, cluster_spec


def apply_cluster(harness: Harness, spec=None, name="demo", annotations=None):
    return harness.apply(C.KIND_RAYCLUSTER, name, copy.deepcopy(spec or cluster_spec()), annotations=annotations)


def test_cluster_becomes_ready(harness):
    apply_cluster(harness)
    harness.settle(30)
    cluster = harness.get(C.KIND_RAYCLUSTER, "demo")
    assert cluster["status"]["state"] == "ready"
    assert cluster["status"]["readyWorkerReplicas"] == 2
    assert cluster["status"]["desiredWorkerReplicas"] == 2
    assert {pod["metadata"]["name"] for pod in harness.list(C.KIND_POD)} == {"demo-head", "demo-workers-0", "demo-workers-1"}
    assert cluster["status"]["head"]["podName"] == "demo-head"
    assert cluster["status"]["endpoints"]["dashboard"].startswith("demo-head-svc.default.svc.cluster.local")


def test_cluster_conditions_and_generation(harness):
    apply_cluster(harness)
    harness.settle(30)
    cluster = harness.get(C.KIND_RAYCLUSTER, "demo")
    conditions = {item["type"]: item for item in cluster["status"]["conditions"]}
    assert conditions[C.COND_HEAD_READY]["status"] == "True"
    assert conditions[C.COND_PROVISIONED]["status"] == "True"
    assert cluster["status"]["observedGeneration"] == cluster["metadata"]["generation"]


def test_cluster_status_resources(harness):
    apply_cluster(harness)
    harness.settle(30)
    status = harness.get(C.KIND_RAYCLUSTER, "demo")["status"]
    assert status["desiredCPU"] == "5"
    assert status["desiredMemory"] == "5Gi"
    assert status["desiredGPU"] == "0"


def test_scale_up_manually(harness):
    apply_cluster(harness, cluster_spec(workers=1, min_replicas=1))
    harness.settle(30)
    cluster = harness.get(C.KIND_RAYCLUSTER, "demo")
    cluster["spec"]["workerGroupSpecs"][0]["replicas"] = 3
    cluster["spec"]["workerGroupSpecs"][0]["maxReplicas"] = 3
    harness.store.update(C.KIND_RAYCLUSTER, cluster)
    harness.settle(30)
    assert len([pod for pod in harness.list(C.KIND_POD) if pod["metadata"]["labels"].get(C.LABEL_NODE_TYPE) == C.NODE_TYPE_WORKER]) == 3
    assert harness.get(C.KIND_RAYCLUSTER, "demo")["status"]["state"] == "ready"


def test_scale_down_manually(harness):
    apply_cluster(harness, cluster_spec(workers=3, min_replicas=0))
    harness.settle(30)
    cluster = harness.get(C.KIND_RAYCLUSTER, "demo")
    cluster["spec"]["workerGroupSpecs"][0]["replicas"] = 1
    harness.store.update(C.KIND_RAYCLUSTER, cluster)
    harness.settle(30)
    workers = [pod for pod in harness.list(C.KIND_POD) if pod["metadata"]["labels"].get(C.LABEL_NODE_TYPE) == C.NODE_TYPE_WORKER]
    assert len(workers) == 1


def test_worker_replicas_clamped_to_max(harness):
    spec = cluster_spec(workers=10, min_replicas=0, max_replicas=2)
    apply_cluster(harness, spec)
    harness.settle(30)
    workers = [pod for pod in harness.list(C.KIND_POD) if pod["metadata"]["labels"].get(C.LABEL_NODE_TYPE) == C.NODE_TYPE_WORKER]
    assert len(workers) == 2


def test_suspend_and_resume(harness):
    apply_cluster(harness)
    harness.settle(30)
    cluster = harness.get(C.KIND_RAYCLUSTER, "demo")
    cluster["spec"]["suspend"] = True
    harness.store.update(C.KIND_RAYCLUSTER, cluster)
    harness.settle(30)
    suspended = harness.get(C.KIND_RAYCLUSTER, "demo")
    assert suspended["status"]["state"] == "suspended"
    assert harness.list(C.KIND_POD) == []
    conditions = {item["type"]: item["status"] for item in suspended["status"]["conditions"]}
    assert conditions[C.COND_SUSPENDED] == "True"
    suspended["spec"]["suspend"] = False
    harness.store.update(C.KIND_RAYCLUSTER, suspended)
    harness.settle(30)
    assert harness.get(C.KIND_RAYCLUSTER, "demo")["status"]["state"] == "ready"
    assert len(harness.list(C.KIND_POD)) == 3


def test_delete_cluster_cleans_children(harness):
    apply_cluster(harness)
    harness.settle(30)
    harness.store.delete(C.KIND_RAYCLUSTER, "demo", "default")
    harness.settle(30)
    assert not harness.store.exists(C.KIND_RAYCLUSTER, "demo", "default")
    assert harness.list(C.KIND_POD) == []
    assert harness.list(C.KIND_SERVICE) == []
    assert harness.runtime.ready_nodes("default", "demo") == []


def test_invalid_spec_sets_validation_status(harness):
    spec = cluster_spec()
    spec["workerGroupSpecs"][0]["minReplicas"] = 5
    spec["workerGroupSpecs"][0]["maxReplicas"] = 1
    apply_cluster(harness, spec)
    harness.settle(10)
    cluster = harness.get(C.KIND_RAYCLUSTER, "demo")
    assert cluster["status"]["reason"] == "ValidationFailed"
    assert cluster["status"]["message"]
    assert harness.list(C.KIND_POD) == []


def test_fixing_invalid_spec_clears_validation_reason(harness):
    spec = cluster_spec()
    spec["workerGroupSpecs"][0]["minReplicas"] = 5
    spec["workerGroupSpecs"][0]["maxReplicas"] = 1
    apply_cluster(harness, spec)
    harness.settle(10)
    assert harness.get(C.KIND_RAYCLUSTER, "demo")["status"]["reason"] == "ValidationFailed"

    cluster = harness.get(C.KIND_RAYCLUSTER, "demo")
    cluster["spec"]["workerGroupSpecs"][0]["minReplicas"] = 1
    cluster["spec"]["workerGroupSpecs"][0]["maxReplicas"] = 3
    harness.store.update(C.KIND_RAYCLUSTER, cluster)
    harness.settle(40)

    status = harness.get(C.KIND_RAYCLUSTER, "demo")["status"]
    assert status["state"] == "ready"
    assert "reason" not in status
    assert "message" not in status


def test_recreate_upgrade(harness):
    spec = cluster_spec(workers=1, min_replicas=1)
    spec["upgradeStrategy"] = {"type": "Recreate"}
    apply_cluster(harness, spec)
    harness.settle(30)
    old_image = harness.get(C.KIND_POD, "demo-head")["spec"]["containers"][0]["image"]
    cluster = harness.get(C.KIND_RAYCLUSTER, "demo")
    cluster["spec"]["headGroupSpec"]["template"]["spec"]["containers"][0]["image"] = "rayproject/ray:2.60.0"
    harness.store.update(C.KIND_RAYCLUSTER, cluster)
    harness.settle(40)
    new_image = harness.get(C.KIND_POD, "demo-head")["spec"]["containers"][0]["image"]
    assert old_image != new_image
    assert new_image.endswith("2.60.0")


def test_none_upgrade_keeps_existing_pods(harness):
    spec = cluster_spec(workers=1, min_replicas=1)
    spec["upgradeStrategy"] = {"type": "None"}
    apply_cluster(harness, spec)
    harness.settle(30)
    cluster = harness.get(C.KIND_RAYCLUSTER, "demo")
    cluster["spec"]["headGroupSpec"]["template"]["spec"]["containers"][0]["image"] = "rayproject/ray:2.60.0"
    harness.store.update(C.KIND_RAYCLUSTER, cluster)
    harness.settle(30)
    assert harness.get(C.KIND_POD, "demo-head")["spec"]["containers"][0]["image"].endswith("2.59.0")


def test_autoscaler_scale_up_and_down(harness):
    spec = cluster_spec(workers=0, min_replicas=0, autoscaling=True)
    apply_cluster(harness, spec)
    harness.settle(20)
    harness.runtime.add_demand("default", "demo", cpu=6, count=1, ttl_seconds=120)
    harness.step(20, advance=0.2)
    cluster = harness.get(C.KIND_RAYCLUSTER, "demo")
    assert cluster["spec"]["workerGroupSpecs"][0]["replicas"] == 3
    assert len([p for p in harness.list(C.KIND_POD) if p["metadata"]["labels"].get(C.LABEL_NODE_TYPE) == C.NODE_TYPE_WORKER]) == 3
    harness.runtime.clear_demand("default", "demo")
    harness.step(80, advance=1.0)
    cluster = harness.get(C.KIND_RAYCLUSTER, "demo")
    assert cluster["spec"]["workerGroupSpecs"][0]["replicas"] == 0
    assert [p for p in harness.list(C.KIND_POD) if p["metadata"]["labels"].get(C.LABEL_NODE_TYPE) == C.NODE_TYPE_WORKER] == []


def test_multi_host_and_headless_service(harness):
    spec = cluster_spec(workers=2, min_replicas=2, num_hosts=2)
    apply_cluster(harness, spec)
    harness.settle(40)
    workers = [p for p in harness.list(C.KIND_POD) if p["metadata"]["labels"].get(C.LABEL_NODE_TYPE) == C.NODE_TYPE_WORKER]
    assert len(workers) == 4
    labels = [worker["metadata"]["labels"] for worker in workers]
    # Two logical replicas, each with two hosts.  Replica index is not the Pod
    # index: the host index distinguishes Pods inside one replica.
    assert {item[C.LABEL_WORKER_REPLICA_INDEX] for item in labels} == {"0", "1"}
    assert {item[C.LABEL_HOST_INDEX] for item in labels} == {"0", "1"}
    assert len({item[C.LABEL_WORKER_REPLICA_NAME] for item in labels}) == 2
    assert {worker["metadata"]["name"] for worker in workers} == {
        "demo-workers-0-0",
        "demo-workers-0-1",
        "demo-workers-1-0",
        "demo-workers-1-1",
    }
    assert harness.store.exists(C.KIND_SERVICE, "demo-headless", "default")


def test_enable_serve_service_annotation(harness, spec_factory):
    spec = cluster_spec(workers=1, min_replicas=1)
    harness.apply(C.KIND_RAYCLUSTER, "demo", spec, annotations={C.ANNOTATION_ENABLE_SERVE_SERVICE: "true"})
    harness.settle(30)
    assert harness.store.exists(C.KIND_SERVICE, "demo-serve-svc", "default")


def test_custom_head_service_name(harness):
    spec = cluster_spec(workers=0)
    spec["headGroupSpec"]["headService"] = {"metadata": {"name": "my-head"}}
    apply_cluster(harness, spec)
    harness.settle(30)
    assert harness.store.exists(C.KIND_SERVICE, "my-head", "default")
    assert harness.get(C.KIND_RAYCLUSTER, "demo")["status"]["head"]["serviceName"] == "my-head"


def test_head_and_worker_pod_environment(harness):
    apply_cluster(harness, cluster_spec(workers=1, min_replicas=1))
    harness.settle(30)
    head = harness.get(C.KIND_POD, "demo-head")
    worker = harness.get(C.KIND_POD, "demo-workers-0")
    head_env = {item["name"]: item.get("value") for item in head["spec"]["containers"][0]["env"]}
    worker_env = {item["name"]: item.get("value") for item in worker["spec"]["containers"][0]["env"]}
    assert head_env[C.RAY_CLUSTER_NAME] == "demo"
    assert head_env[C.RAY_NODE_TYPE] == "head"
    assert worker_env[C.RAY_NODE_TYPE] == "worker"
    assert "ray start --head" in " ".join(head["spec"]["containers"][0]["args"])
    assert "--address=demo-head-svc" in " ".join(worker["spec"]["containers"][0]["args"])


def test_gcs_fault_tolerance_rocksdb(harness):
    harness.config.feature_gates["GCSFaultToleranceEmbeddedStorage"] = True
    spec = cluster_spec(workers=0)
    spec["gcsFaultToleranceOptions"] = {"backend": "rocksdb", "storage": {"size": "2Gi"}}
    apply_cluster(harness, spec)
    harness.settle(30)
    assert harness.store.exists(C.KIND_PVC, "demo-gcs-pvc", "default")
    head = harness.get(C.KIND_POD, "demo-head")
    env = {item["name"]: item.get("value") for item in head["spec"]["containers"][0]["env"]}
    assert env[C.RAY_GCS_STORAGE] == "rocksdb"
    assert env[C.RAY_GCS_STORAGE_PATH] == "/data/gcs"


def test_network_policy_feature_gate(harness):
    spec = cluster_spec(workers=0)
    spec["networkPolicy"] = {"mode": "DenyAll"}
    apply_cluster(harness, spec)
    harness.settle(10)
    assert harness.get(C.KIND_RAYCLUSTER, "demo")["status"]["reason"] == "ValidationFailed"
    harness.config.feature_gates["RayClusterNetworkPolicy"] = True
    cluster = harness.get(C.KIND_RAYCLUSTER, "demo")
    cluster["status"] = {}
    harness.store.update(C.KIND_RAYCLUSTER, cluster)
    harness.settle(20)
    policies = harness.list(C.KIND_NETWORKPOLICY)
    assert {policy["metadata"]["name"] for policy in policies} == {"demo-head", "demo-worker"}


def test_auth_secret_created(harness):
    spec = cluster_spec(workers=0)
    spec["authOptions"] = {"mode": "token"}
    apply_cluster(harness, spec)
    harness.settle(30)
    assert harness.store.exists(C.KIND_SECRET, "demo-auth", "default")


def test_mtls_secret_and_env(harness):
    harness.config.feature_gates["RayClusterMTLS"] = True
    spec = cluster_spec(workers=0)
    spec["tlsOptions"] = {"enabled": True}
    apply_cluster(harness, spec)
    harness.settle(30)
    assert harness.store.exists(C.KIND_SECRET, "demo-head-tls", "default")
    head = harness.get(C.KIND_POD, "demo-head")
    env = {item["name"]: item.get("value") for item in head["spec"]["containers"][0]["env"]}
    assert env[C.RAY_USE_TLS] == "1"
    assert any(mount["mountPath"] == "/etc/ray/tls" for mount in head["spec"]["containers"][0].get("volumeMounts", []))


def test_multi_host_replica_is_replaced_as_a_unit(harness):
    spec = cluster_spec(workers=1, min_replicas=1, num_hosts=2)
    apply_cluster(harness, spec)
    harness.settle(40)
    workers = [p for p in harness.list(C.KIND_POD) if p["metadata"]["labels"].get(C.LABEL_NODE_TYPE) == C.NODE_TYPE_WORKER]
    assert len(workers) == 2
    victim = harness.get(C.KIND_POD, "demo-workers-0-1")
    victim["status"]["phase"] = "Failed"
    harness.store.update(C.KIND_POD, victim)
    harness.settle(40)
    workers = [p for p in harness.list(C.KIND_POD) if p["metadata"]["labels"].get(C.LABEL_NODE_TYPE) == C.NODE_TYPE_WORKER]
    # Both hosts are recreated, not just the failed one.
    assert len(workers) == 2
    assert {pod_phase_name(p) for p in workers} == {"Running"}
    assert len({p["metadata"]["labels"][C.LABEL_WORKER_REPLICA_NAME] for p in workers}) == 1


def test_multi_host_scale_down_removes_whole_replicas(harness):
    spec = cluster_spec(workers=3, min_replicas=0, num_hosts=2)
    apply_cluster(harness, spec)
    harness.settle(40)
    workers = [p for p in harness.list(C.KIND_POD) if p["metadata"]["labels"].get(C.LABEL_NODE_TYPE) == C.NODE_TYPE_WORKER]
    assert len(workers) == 6
    cluster = harness.get(C.KIND_RAYCLUSTER, "demo")
    cluster["spec"]["workerGroupSpecs"][0]["replicas"] = 1
    harness.store.update(C.KIND_RAYCLUSTER, cluster)
    harness.settle(40)
    workers = [p for p in harness.list(C.KIND_POD) if p["metadata"]["labels"].get(C.LABEL_NODE_TYPE) == C.NODE_TYPE_WORKER]
    assert len(workers) == 2
    assert {p["metadata"]["labels"][C.LABEL_WORKER_REPLICA_INDEX] for p in workers} == {"0"}
    assert {p["metadata"]["labels"][C.LABEL_HOST_INDEX] for p in workers} == {"0", "1"}


def pod_phase_name(pod):
    return pod.get("status", {}).get("phase", "Pending")


def test_custom_head_service_ports_are_merged_by_name(harness):
    spec = cluster_spec(workers=0)
    spec["headGroupSpec"]["headService"] = {
        "metadata": {"name": "custom-head"},
        "spec": {"ports": [{"name": "dashboard", "port": 9000, "targetPort": 8265}]},
    }
    apply_cluster(harness, spec)
    harness.settle(30)
    service = harness.get(C.KIND_SERVICE, "custom-head")
    names = [port["name"] for port in service["spec"]["ports"]]
    assert len(names) == len(set(names))
    dashboard = next(port for port in service["spec"]["ports"] if port["name"] == "dashboard")
    assert dashboard["port"] == 9000
    assert "gcs-server" in names and "serve" in names


def test_ingress_is_created_and_pruned(harness):
    spec = cluster_spec(workers=0)
    spec["headGroupSpec"]["enableIngress"] = True
    spec["headGroupSpec"]["ingressOptions"] = {
        "host": "ray.example.com",
        "path": "/dashboard",
        "pathType": "Prefix",
        "ingressClassName": "nginx",
        "tls": [{"hosts": ["ray.example.com"], "secretName": "ray-tls"}],
    }
    apply_cluster(harness, spec)
    harness.settle(30)
    ingress = harness.get(C.KIND_INGRESS, "demo-ingress")
    assert ingress["spec"]["ingressClassName"] == "nginx"
    rule = ingress["spec"]["rules"][0]
    assert rule["host"] == "ray.example.com"
    path = rule["http"]["paths"][0]
    assert path["path"] == "/dashboard"
    assert path["backend"]["service"]["name"] == "demo-head-svc"
    assert ingress["spec"]["tls"][0]["secretName"] == "ray-tls"

    cluster = harness.get(C.KIND_RAYCLUSTER, "demo")
    cluster["spec"]["headGroupSpec"]["enableIngress"] = False
    harness.store.update(C.KIND_RAYCLUSTER, cluster)
    harness.settle(30)
    assert not harness.store.exists(C.KIND_INGRESS, "demo-ingress", "default")


def test_replica_failure_condition_is_set_and_cleared(harness, monkeypatch):
    import minikuberay.controllers.raycluster as raycluster_module

    original = raycluster_module.build_head_pod

    def explode(*args, **kwargs):
        raise RuntimeError("simulated quota failure")

    monkeypatch.setattr(raycluster_module, "build_head_pod", explode)
    apply_cluster(harness, cluster_spec(workers=0))
    harness.settle(10)
    cluster = harness.get(C.KIND_RAYCLUSTER, "demo")
    conditions = {item["type"]: item for item in cluster["status"].get("conditions") or []}
    assert conditions[C.COND_REPLICA_FAILURE]["status"] == "True"
    assert "simulated quota failure" in conditions[C.COND_REPLICA_FAILURE]["message"]

    monkeypatch.setattr(raycluster_module, "build_head_pod", original)
    harness.settle(30)
    cluster = harness.get(C.KIND_RAYCLUSTER, "demo")
    conditions = {item["type"]: item for item in cluster["status"].get("conditions") or []}
    assert conditions[C.COND_REPLICA_FAILURE]["status"] == "False"
    assert cluster["status"]["state"] == "ready"


def test_gcs_ft_redis_cleanup_finalizer_blocks_delete_until_job_completes(harness):
    spec = cluster_spec(workers=1, min_replicas=1)
    spec["gcsFaultToleranceOptions"] = {
        "backend": "redis",
        "redisAddress": "redis.default.svc.cluster.local:6379",
        "externalStorageNamespace": "demo",
    }
    apply_cluster(harness, spec)
    harness.settle(30)
    cluster = harness.get(C.KIND_RAYCLUSTER, "demo")
    assert C.FINALIZER_GCS_FT in cluster["metadata"]["finalizers"]

    harness.store.delete(C.KIND_RAYCLUSTER, "demo", "default")
    # The finalizer must keep the CR around while the cleanup Job runs.
    harness.step(1)
    assert harness.store.exists(C.KIND_RAYCLUSTER, "demo", "default")
    assert harness.store.exists(C.KIND_JOB, "demo-redis-cleanup", "default")
    harness.step(60, advance=0.1)
    harness.settle(60)
    assert not harness.store.exists(C.KIND_RAYCLUSTER, "demo", "default")
    assert not harness.store.exists(C.KIND_JOB, "demo-redis-cleanup", "default")


def test_gcs_ft_cleanup_timeout_releases_finalizer(harness, monkeypatch):
    import minikuberay.controllers.raycluster as raycluster_module

    def stuck_job(cluster):
        job = original(cluster)
        job["spec"]["template"]["metadata"]["annotations"][C.ANNOTATION_SIM_JOB_COMPLETE] = "99999"
        return job

    original = raycluster_module.build_redis_cleanup_job
    monkeypatch.setattr(raycluster_module, "build_redis_cleanup_job", stuck_job)
    spec = cluster_spec(workers=0)
    spec["gcsFaultToleranceOptions"] = {"backend": "redis", "redisAddress": "redis:6379"}
    harness.apply(
        C.KIND_RAYCLUSTER,
        "stuck",
        spec,
        annotations={C.ANNOTATION_GCS_FT_DELETION_TIMEOUT: "1s"},
    )
    harness.settle(20)
    harness.store.delete(C.KIND_RAYCLUSTER, "stuck", "default")
    harness.step(30, advance=0.5)
    assert not harness.store.exists(C.KIND_RAYCLUSTER, "stuck", "default")
    assert any(
        event.get("reason") == "GCSFTRedisCleanupTimeout"
        for event in harness.list(C.KIND_EVENT)
    )


def test_invalid_spec_event_is_emitted_once(harness):
    spec = cluster_spec(workers=1)
    spec["workerGroupSpecs"][0]["minReplicas"] = 5
    spec["workerGroupSpecs"][0]["maxReplicas"] = 1
    harness.apply(C.KIND_RAYCLUSTER, "bad", spec)
    harness.step(3)
    events = [
        event
        for event in harness.list(C.KIND_EVENT)
        if event.get("reason") == "InvalidRayClusterSpec"
    ]
    assert len(events) == 1
    # The status write converges; the next scan must not recreate the event.
    assert harness.operator.run_once() is False
