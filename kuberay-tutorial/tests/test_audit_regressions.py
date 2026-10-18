"""Regression tests for issues found by the independent implementation audit.

Each test names the behavior that used to be wrong.  They are intentionally
small and deterministic; the goal is to prevent the old behavior from coming
back while the tutorial evolves.
"""

from __future__ import annotations

import copy

import pytest

from minikuberay import constants as C
from minikuberay.apiserver.server import ApiServer
from minikuberay.kube.client import HTTPClient
from minikuberay.models.core import new_pod
from minikuberay.models.meta import new_object
from tests.conftest import Harness, cluster_spec
from tests.test_rayservice import bump_cluster_image, run, serve_config, service_spec


def job_spec(**overrides):
    spec = {
        "entrypoint": "python -c \"print('hi')\"",
        "submissionMode": "HTTPMode",
        "rayClusterSpec": cluster_spec(workers=0),
    }
    spec.update(overrides)
    return spec


def apply_job(harness: Harness, spec: dict, name: str = "job"):
    return harness.create(new_object(C.RAY_API_VERSION, C.KIND_RAYJOB, name, "default", spec=copy.deepcopy(spec)))


def test_cluster_selector_never_deletes_shared_cluster(harness):
    harness.apply(C.KIND_RAYCLUSTER, "shared", cluster_spec(workers=1, min_replicas=1))
    harness.settle(30)

    invalid = job_spec(
        clusterSelector={C.LABEL_CLUSTER: "shared"},
        shutdownAfterJobFinishes=True,
    )
    invalid.pop("rayClusterSpec")
    apply_job(harness, invalid, "invalid")
    harness.settle(20)
    assert harness.get(C.KIND_RAYJOB, "invalid")["status"]["jobDeploymentStatus"] == "ValidationFailed"
    assert harness.store.exists(C.KIND_RAYCLUSTER, "shared", "default")

    valid = job_spec(clusterSelector={C.LABEL_CLUSTER: "shared"}, shutdownAfterJobFinishes=False)
    valid.pop("rayClusterSpec")
    apply_job(harness, valid, "valid")
    harness.step(80, advance=0.5)
    assert harness.get(C.KIND_RAYJOB, "valid")["status"]["jobDeploymentStatus"] in ("Complete", "Failed")
    assert harness.store.exists(C.KIND_RAYCLUSTER, "shared", "default")


def test_validation_failure_recovers_after_spec_fix(harness):
    broken = job_spec(submissionMode="NotAMode")
    apply_job(harness, broken)
    harness.settle(10)
    assert harness.get(C.KIND_RAYJOB, "job")["status"]["jobDeploymentStatus"] == "ValidationFailed"

    job = harness.get(C.KIND_RAYJOB, "job")
    job["spec"]["submissionMode"] = "HTTPMode"
    harness.store.update(C.KIND_RAYJOB, job)
    harness.step(80, advance=0.5)
    assert harness.get(C.KIND_RAYJOB, "job")["status"]["jobDeploymentStatus"] in ("Complete", "Failed")
    assert harness.get(C.KIND_RAYJOB, "job")["status"]["jobDeploymentStatus"] != "ValidationFailed"


def test_rayservice_recovers_when_active_cluster_is_deleted_during_upgrade(harness):
    spec = service_spec(rayClusterConfig=cluster_spec(workers=1, min_replicas=1))
    obj = new_object(
        C.RAY_API_VERSION,
        C.KIND_RAYSERVICE,
        "svc",
        "default",
        spec=spec,
        annotations={C.ANNOTATION_SIM_DEPLOY_DELAY: "30"},
    )
    harness.create(obj)
    run(harness)
    old_active = harness.get(C.KIND_RAYSERVICE, "svc")["status"]["activeServiceStatus"]["rayClusterName"]
    service = harness.get(C.KIND_RAYSERVICE, "svc")
    bump_cluster_image(service, "2.60.0")
    harness.store.update(C.KIND_RAYSERVICE, service)
    run(harness, 15)
    pending_status = harness.get(C.KIND_RAYSERVICE, "svc")["status"].get("pendingServiceStatus") or {}
    pending = pending_status.get("rayClusterName")
    assert pending and pending != old_active

    harness.store.delete(C.KIND_RAYCLUSTER, old_active, "default")
    run(harness, 80, 0.5)
    status = harness.get(C.KIND_RAYSERVICE, "svc")["status"]
    assert status["activeServiceStatus"]["rayClusterName"] == pending
    assert status["serviceStatus"] == "Running"


def test_incremental_pending_cluster_has_serve_endpoints(harness):
    spec = service_spec(
        rayClusterConfig=cluster_spec(workers=1, min_replicas=1, autoscaling=True),
        upgradeStrategy={
            "type": "NewClusterWithIncrementalUpgrade",
            "clusterUpgradeOptions": {
                "maxSurgePercent": 100,
                "stepSizePercent": 50,
                "intervalSeconds": 5,
                "gatewayClassName": "gateway",
            },
        },
    )
    harness.create(new_object(C.RAY_API_VERSION, C.KIND_RAYSERVICE, "svc", "default", spec=spec))
    run(harness)
    service = harness.get(C.KIND_RAYSERVICE, "svc")
    bump_cluster_image(service, "2.60.0")
    harness.store.update(C.KIND_RAYSERVICE, service)
    run(harness, 12, 0.5)
    status = harness.get(C.KIND_RAYSERVICE, "svc")["status"]
    pending = status["pendingServiceStatus"]["rayClusterName"]
    pending_pods = harness.list(C.KIND_POD, label_selector={C.LABEL_CLUSTER: pending})
    serve_pods = [pod for pod in pending_pods if (pod["metadata"].get("labels") or {}).get(C.LABEL_SERVE) == "true"]
    assert serve_pods, "pending cluster must have endpoints before receiving traffic"
    assert status["pendingServiceStatus"]["trafficRoutedPercent"] > 0


def test_recreate_ignores_replica_bounds(harness):
    spec = cluster_spec(workers=1, min_replicas=1)
    spec["upgradeStrategy"] = {"type": "Recreate"}
    harness.apply(C.KIND_RAYCLUSTER, "demo", spec)
    harness.settle(30)
    uids = {pod["metadata"]["name"]: pod["metadata"]["uid"] for pod in harness.list(C.KIND_POD)}

    cluster = harness.get(C.KIND_RAYCLUSTER, "demo")
    cluster["spec"]["workerGroupSpecs"][0]["maxReplicas"] = 10
    harness.store.update(C.KIND_RAYCLUSTER, cluster)
    harness.settle(30)
    assert {pod["metadata"]["name"]: pod["metadata"]["uid"] for pod in harness.list(C.KIND_POD)} == uids


def test_recreate_replaces_workers_even_after_head_deletion(harness):
    spec = cluster_spec(workers=1, min_replicas=1)
    spec["upgradeStrategy"] = {"type": "Recreate"}
    harness.apply(C.KIND_RAYCLUSTER, "demo", spec)
    harness.settle(30)
    harness.store.delete(C.KIND_POD, "demo-head", "default", force=True)
    harness.settle(20)
    cluster = harness.get(C.KIND_RAYCLUSTER, "demo")
    for container in cluster["spec"]["headGroupSpec"]["template"]["spec"]["containers"]:
        container["image"] = "rayproject/ray:2.60.0"
    for group in cluster["spec"]["workerGroupSpecs"]:
        for container in group["template"]["spec"]["containers"]:
            container["image"] = "rayproject/ray:2.60.0"
    harness.store.update(C.KIND_RAYCLUSTER, cluster)
    harness.settle(40)
    for pod in harness.list(C.KIND_POD):
        assert pod["spec"]["containers"][0]["image"] == "rayproject/ray:2.60.0"


def test_autoscaler_accounts_for_num_hosts(harness):
    spec = cluster_spec(workers=0, min_replicas=0, autoscaling=True, worker_cpu="2", num_hosts=2)
    spec["workerGroupSpecs"][0]["maxReplicas"] = 8
    harness.apply(C.KIND_RAYCLUSTER, "multi", spec)
    harness.settle(20)
    harness.runtime.add_demand("default", "multi", cpu=8, count=1, ttl_seconds=120)
    harness.step(12, advance=0.2)
    # 8 cores / (2 cores per Pod * 2 hosts) = 2 replicas = 4 Pods.
    cluster = harness.get(C.KIND_RAYCLUSTER, "multi")
    assert cluster["spec"]["workerGroupSpecs"][0]["replicas"] == 2
    workers = [p for p in harness.list(C.KIND_POD) if (p["metadata"].get("labels") or {}).get(C.LABEL_NODE_TYPE) == C.NODE_TYPE_WORKER]
    assert len(workers) == 4


def test_auth_env_is_injected_into_workers(harness):
    spec = cluster_spec(workers=1, min_replicas=1)
    spec["authOptions"] = {"mode": "token"}
    harness.apply(C.KIND_RAYCLUSTER, "auth", spec)
    harness.settle(30)
    worker = harness.get(C.KIND_POD, "auth-workers-0")
    env = {item["name"]: item for item in worker["spec"]["containers"][0].get("env") or []}
    assert env[C.RAY_AUTH_MODE]["value"] == "token"
    assert "secretKeyRef" in env[C.RAY_AUTH_TOKEN]["valueFrom"]


def test_patch_status_cannot_change_spec_and_rename_is_rejected(harness):
    server = ApiServer(harness.store, runtime=harness.runtime, port=0)
    url = server.start()
    try:
        client = HTTPClient(url)
        client.create(C.KIND_RAYCLUSTER, new_object(C.RAY_API_VERSION, C.KIND_RAYCLUSTER, "a", "default", spec=cluster_spec(workers=0)))
        patched = client._request(
            "PATCH",
            "/apis/ray.io/v1/namespaces/default/rayclusters/a/status",
            {"spec": {"rayVersion": "9.9.9"}, "status": {"state": "ready"}},
        )
        assert patched["status"]["state"] == "ready"
        assert patched["spec"]["rayVersion"] == "2.59.0"
        with pytest.raises(Exception):
            client._request(
                "PATCH",
                "/apis/ray.io/v1/namespaces/default/rayclusters/a",
                {"metadata": {"name": "b"}},
            )
    finally:
        server.stop()


def test_malformed_pod_does_not_stall_other_pods(harness):
    good = new_pod(
        "good",
        "default",
        {"containers": [{"name": "ray", "image": "x", "resources": {"requests": {"cpu": "100m", "memory": "64Mi"}}}]},
    )
    bad = new_pod(
        "bad",
        "default",
        {"containers": [{"name": "ray", "image": "x", "resources": {"requests": {"cpu": "not-a-number"}}}]},
    )
    harness.store.create(C.KIND_POD, good)
    harness.store.create(C.KIND_POD, bad)
    harness.step(5)
    assert harness.get(C.KIND_POD, "good")["status"]["phase"] == "Running"
    assert harness.get(C.KIND_POD, "bad")["status"]["phase"] == "Pending"


def test_active_deadline_applies_while_initializing(harness):
    spec = cluster_spec(workers=1, min_replicas=1, worker_cpu="1")
    spec["workerGroupSpecs"][0]["template"]["spec"]["containers"][0]["resources"]["requests"]["nvidia.com/gpu"] = "8"
    job = job_spec(rayClusterSpec=spec, activeDeadlineSeconds=5)
    apply_job(harness, job)
    harness.step(20, advance=1.0)
    status = harness.get(C.KIND_RAYJOB, "job")["status"]
    assert status["jobDeploymentStatus"] == "Failed"
    assert status["reason"] == "DeadlineExceeded"


def test_failed_submitter_fails_the_rayjob(harness):
    # With backoffLimit=0 the first submitter failure is terminal, which is
    # what this regression is about.  Retry semantics are covered separately.
    spec = job_spec(submissionMode="K8sJobMode", submitterConfig={"backoffLimit": 0})
    obj = new_object(
        C.RAY_API_VERSION,
        C.KIND_RAYJOB,
        "job",
        "default",
        spec=copy.deepcopy(spec),
        annotations={C.ANNOTATION_SIM_JOB_DURATION: "10"},
    )
    harness.create(obj)
    harness.step(30, advance=0.2)
    pods = harness.list(C.KIND_POD, label_selector={"job-name": "job-submitter"})
    assert pods
    pod = harness.get(C.KIND_POD, pods[0]["metadata"]["name"])
    env = {item["name"] for item in pod["spec"]["containers"][0].get("env") or []}
    assert {C.RAY_JOB_SUBMISSION_ID, C.RAY_DASHBOARD_ADDRESS, C.FQ_RAY_IP} <= env
    pod["status"]["phase"] = "Failed"
    harness.store.update(C.KIND_POD, pod)
    harness.step(20, advance=0.2)
    status = harness.get(C.KIND_RAYJOB, "job")["status"]
    assert status["jobDeploymentStatus"] == "Failed"
    assert status["reason"] == "SubmissionFailed"


def test_upgrade_strategy_none_still_patches_worker_replicas(harness):
    spec = service_spec(
        rayClusterConfig=cluster_spec(workers=1, min_replicas=1),
        upgradeStrategy={"type": "None"},
    )
    harness.create(new_object(C.RAY_API_VERSION, C.KIND_RAYSERVICE, "svc", "default", spec=spec))
    run(harness)
    active = harness.get(C.KIND_RAYSERVICE, "svc")["status"]["activeServiceStatus"]["rayClusterName"]
    service = harness.get(C.KIND_RAYSERVICE, "svc")
    group = service["spec"]["rayClusterConfig"]["workerGroupSpecs"][0]
    group["replicas"] = 3
    group["maxReplicas"] = 3
    harness.store.update(C.KIND_RAYSERVICE, service)
    run(harness, 40)
    cluster = harness.get(C.KIND_RAYCLUSTER, active)
    assert cluster["spec"]["workerGroupSpecs"][0]["replicas"] == 3
    workers = [p for p in harness.list(C.KIND_POD) if (p["metadata"].get("labels") or {}).get(C.LABEL_NODE_TYPE) == C.NODE_TYPE_WORKER]
    assert len(workers) == 3


def test_upgrade_strategy_none_still_repairs_services(harness):
    """A stale-template cluster must not stop reconciling Services."""
    harness.apply(C.KIND_RAYCLUSTER, "demo", cluster_spec(workers=1, min_replicas=1))
    harness.settle(30)
    harness.store.delete(C.KIND_SERVICE, "demo-head-svc", "default", force=True)
    cluster = harness.get(C.KIND_RAYCLUSTER, "demo")
    cluster["spec"]["upgradeStrategy"] = {"type": "None"}
    cluster["spec"]["headGroupSpec"]["template"]["spec"]["containers"][0]["image"] = "rayproject/ray:2.60.0"
    harness.store.update(C.KIND_RAYCLUSTER, cluster)
    harness.settle(15)
    assert harness.store.exists(C.KIND_SERVICE, "demo-head-svc", "default")


def test_removed_worker_group_deletes_its_pods(harness):
    spec = cluster_spec(workers=1, min_replicas=1)
    extra = copy.deepcopy(spec["workerGroupSpecs"][0])
    extra["groupName"] = "extra"
    extra["replicas"] = 2
    extra["minReplicas"] = 2
    spec["workerGroupSpecs"].append(extra)
    harness.apply(C.KIND_RAYCLUSTER, "demo", spec)
    harness.settle(30)
    assert len([p for p in harness.list(C.KIND_POD) if p["metadata"]["labels"].get(C.LABEL_GROUP) == "extra"]) == 2

    cluster = harness.get(C.KIND_RAYCLUSTER, "demo")
    cluster["spec"]["workerGroupSpecs"] = [g for g in cluster["spec"]["workerGroupSpecs"] if g["groupName"] != "extra"]
    harness.store.update(C.KIND_RAYCLUSTER, cluster)
    harness.settle(20)
    groups = {p["metadata"]["labels"].get(C.LABEL_GROUP) for p in harness.list(C.KIND_POD)}
    assert "extra" not in groups


def test_autoscaler_does_not_flap_with_sustained_demand(harness):
    harness.apply(C.KIND_RAYCLUSTER, "demo", cluster_spec(workers=0, min_replicas=0, autoscaling=True))
    harness.settle(20)
    harness.runtime.add_demand("default", "demo", cpu=6, ttl_seconds=120)
    harness.settle(40, advance=0.5)
    replicas = harness.get(C.KIND_RAYCLUSTER, "demo")["spec"]["workerGroupSpecs"][0]["replicas"]
    assert replicas == 3
    # The demand entry is still unexpired: another few rounds must not scale down.
    harness.step(10, advance=0.5)
    assert harness.get(C.KIND_RAYCLUSTER, "demo")["spec"]["workerGroupSpecs"][0]["replicas"] == 3


def test_store_owns_uid_and_rejects_kind_mutation(harness):
    from minikuberay.errors import BadRequest
    from minikuberay.store.store import ApiStore

    store = ApiStore()
    doc = new_object(C.RAY_API_VERSION, C.KIND_RAYCLUSTER, "a", "default", spec=cluster_spec(workers=0))
    doc["metadata"]["uid"] = "client-supplied"
    created = store.create(C.KIND_RAYCLUSTER, doc, "default")
    assert created["metadata"]["uid"] != "client-supplied"

    second = new_object(C.RAY_API_VERSION, C.KIND_RAYCLUSTER, "b", "default", spec=cluster_spec(workers=0))
    store.create(C.KIND_RAYCLUSTER, second, "default")
    with pytest.raises(BadRequest):
        store.patch(C.KIND_RAYCLUSTER, "b", "default", {"kind": C.KIND_POD})
    # The object must still be readable as a RayCluster.
    assert store.get(C.KIND_RAYCLUSTER, "b", "default")["kind"] == C.KIND_RAYCLUSTER


def test_watch_replay_raises_410_after_compaction():
    from minikuberay.errors import ResourceVersionTooOld
    from minikuberay.store.store import ApiStore

    store = ApiStore(event_log_size=2)
    for index in range(4):
        pod = new_pod(f"p{index}", "default", {"containers": [{"name": "c", "image": "busybox"}]})
        store.create(C.KIND_POD, pod, "default")
    with pytest.raises(ResourceVersionTooOld):
        store.check_watch_resource_version(1)


def test_worker_command_has_one_address(harness):
    from minikuberay.controllers.builders import build_worker_pod

    cluster = new_object(C.RAY_API_VERSION, C.KIND_RAYCLUSTER, "demo", "default", spec=cluster_spec(workers=1))
    group = cluster["spec"]["workerGroupSpecs"][0]
    pod = build_worker_pod(cluster, group, 0)
    args = " ".join(pod["spec"]["containers"][0]["args"])
    assert args.count("--address") == 1


def test_merge_patch_can_remove_labels(harness):
    service = harness.apply(C.KIND_RAYCLUSTER, "demo", cluster_spec(workers=0), labels={"team": "ml"})
    harness.store.patch(C.KIND_RAYCLUSTER, "demo", "default", {"metadata": {"labels": None}})
    assert harness.get(C.KIND_RAYCLUSTER, "demo")["metadata"]["labels"] == {}


def test_helper_pod_is_not_registered_as_ray_node(harness):
    pod = new_pod(
        "cleanup-helper",
        "default",
        {"containers": [{"name": "helper", "image": "redis:7"}]},
        labels={C.LABEL_CLUSTER: "demo"},
    )
    harness.store.create(C.KIND_POD, pod, "default")
    harness.step(5, advance=0.1)
    assert harness.runtime.nodes("default", "demo") == []


def test_submitter_retries_before_failing_rayjob(harness):
    """submitterConfig.backoffLimit must be consumed before the RayJob fails."""
    spec = job_spec(submissionMode="K8sJobMode", submitterConfig={"backoffLimit": 1})
    apply_job(harness, spec)
    # Wait until the first submitter Pod exists, then fail exactly that Pod.
    for _ in range(40):
        pods = harness.list(C.KIND_POD, label_selector={"job-name": "job-submitter"})
        if pods:
            break
        harness.step(1, advance=0.2)
    pod = pods[0]
    pod["status"]["phase"] = "Failed"
    harness.store.update(C.KIND_POD, pod)
    harness.step(40, advance=0.2)
    job = harness.get(C.KIND_RAYJOB, "job")
    # The retry succeeded; the RayJob must not be stuck in Failed from the
    # first submitter attempt.
    assert job["status"]["jobDeploymentStatus"] in ("Complete", "Running")
    assert job["status"].get("jobDeploymentStatus") != "Failed"
