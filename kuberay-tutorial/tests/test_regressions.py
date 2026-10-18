from __future__ import annotations

import copy

import pytest

from minikuberay import constants as C
from minikuberay.apiserver.server import ApiServer
from minikuberay.kube.client import HTTPClient
from minikuberay.models.core import new_pod
from minikuberay.models.meta import new_object
from tests.conftest import Harness, cluster_spec


def test_post_status_is_ignored():
    harness = Harness()
    server = ApiServer(harness.store, runtime=harness.runtime, port=0)
    url = server.start()
    try:
        obj = new_object(C.RAY_API_VERSION, C.KIND_RAYCLUSTER, "status-post", "default", spec=cluster_spec(workers=0))
        obj["status"] = {"state": "ready", "hacked": True}
        client = HTTPClient(url)
        client.create(C.KIND_RAYCLUSTER, obj)
        current = harness.get(C.KIND_RAYCLUSTER, "status-post")
        assert current.get("status", {}).get("state") != "ready"
        assert "hacked" not in current.get("status", {})
    finally:
        server.stop()


def test_put_rejects_mismatched_kind():
    harness = Harness()
    server = ApiServer(harness.store, runtime=harness.runtime, port=0)
    url = server.start()
    try:
        client = HTTPClient(url)
        client.create(C.KIND_RAYCLUSTER, new_object(C.RAY_API_VERSION, C.KIND_RAYCLUSTER, "kind-test", "default", spec=cluster_spec(workers=0)))
        body = new_object(C.RAY_API_VERSION, C.KIND_RAYJOB, "kind-test", "default", spec={"entrypoint": "echo"})
        with pytest.raises(Exception):
            client._request("PUT", "/apis/ray.io/v1/namespaces/default/rayclusters/kind-test", body)
        assert harness.get(C.KIND_RAYCLUSTER, "kind-test")["kind"] == C.KIND_RAYCLUSTER
    finally:
        server.stop()


def test_retained_gcs_pvc_survives_cluster_delete():
    harness = Harness(feature_gates={"GCSFaultToleranceEmbeddedStorage": True})
    spec = cluster_spec(workers=0)
    spec["gcsFaultToleranceOptions"] = {
        "backend": "rocksdb",
        "storage": {"size": "2Gi", "deletionPolicy": "Retain"},
    }
    harness.apply(C.KIND_RAYCLUSTER, "retained", spec)
    harness.settle(30)
    assert harness.store.exists(C.KIND_PVC, "retained-gcs-pvc", "default")
    harness.store.delete(C.KIND_RAYCLUSTER, "retained", "default")
    harness.settle(30)
    assert not harness.store.exists(C.KIND_RAYCLUSTER, "retained", "default")
    assert harness.store.exists(C.KIND_PVC, "retained-gcs-pvc", "default")


def test_workers_to_delete_cannot_touch_unrelated_pod():
    harness = Harness()
    harness.apply(C.KIND_RAYCLUSTER, "safe", cluster_spec(workers=1, min_replicas=1))
    harness.settle(30)
    victim = new_pod("victim", "default", {"containers": [{"name": "ray", "image": "x", "resources": {"requests": {"cpu": "100m"}}}]}, labels={"app": "other"})
    harness.store.create(C.KIND_POD, victim)
    cluster = harness.get(C.KIND_RAYCLUSTER, "safe")
    cluster["spec"]["workerGroupSpecs"][0]["scaleStrategy"] = {"workersToDelete": ["victim"]}
    harness.store.update(C.KIND_RAYCLUSTER, cluster)
    harness.settle(20)
    assert harness.store.exists(C.KIND_POD, "victim", "default")


def test_cron_resume_clears_suspended_status():
    harness = Harness(feature_gates={"RayCronJob": True})
    cron_spec = {
        "schedule": "*/5 * * * *",
        "timeZone": "UTC",
        "suspend": True,
        "jobTemplate": {"entrypoint": "echo", "rayClusterSpec": cluster_spec(workers=0)},
    }
    harness.apply(C.KIND_RAYCRONJOB, "resume", cron_spec)
    harness.step(3, 60)
    assert harness.get(C.KIND_RAYCRONJOB, "resume")["status"].get("suspended") is True
    cron = harness.get(C.KIND_RAYCRONJOB, "resume")
    cron["spec"]["suspend"] = False
    harness.store.update(C.KIND_RAYCRONJOB, cron)
    harness.step(8, 60)
    status = harness.get(C.KIND_RAYCRONJOB, "resume")["status"]
    assert status.get("suspended") is False
    assert harness.list(C.KIND_RAYJOB, label_selector={C.LABEL_CRONJOB_NAME: "resume"})


def test_autoscaler_does_not_double_provision():
    harness = Harness()
    spec = {
        "rayVersion": "2.59.0",
        "enableInTreeAutoscaling": True,
        "autoscalerOptions": {"idleTimeoutSeconds": 30},
        "headGroupSpec": {
            "rayStartParams": {},
            "template": {"spec": {"containers": [{"name": "ray", "image": "x", "resources": {"requests": {"cpu": "1"}}}]}},
        },
        "workerGroupSpecs": [
            {
                "groupName": "existing",
                "replicas": 2,
                "minReplicas": 2,
                "maxReplicas": 4,
                "priority": 10,
                "rayStartParams": {},
                "template": {"spec": {"containers": [{"name": "ray", "image": "x", "resources": {"requests": {"cpu": "1"}}}]}},
            },
            {
                "groupName": "spare",
                "replicas": 0,
                "minReplicas": 0,
                "maxReplicas": 4,
                "priority": 0,
                "rayStartParams": {},
                "template": {"spec": {"containers": [{"name": "ray", "image": "x", "resources": {"requests": {"cpu": "1"}}}]}},
            },
        ],
    }
    harness.apply(C.KIND_RAYCLUSTER, "multi", spec)
    harness.settle(20)
    harness.runtime.add_demand("default", "multi", cpu=1, count=1, ttl_seconds=120)
    harness.step(10, 0.2)
    cluster = harness.get(C.KIND_RAYCLUSTER, "multi")
    groups = {group["groupName"]: group for group in cluster["spec"]["workerGroupSpecs"]}
    assert groups["spare"]["replicas"] == 0


def test_batch_job_terminal_status_does_not_churn():
    from minikuberay.models.core import new_job

    harness = Harness()
    job = new_job("done", "default", {"spec": {"containers": [{"name": "s", "image": "x"}]}}, labels={})
    job["status"] = {"active": 0, "succeeded": 0, "failed": 1, "conditions": [{"type": "Failed", "status": "True", "reason": "BackoffLimitExceeded"}]}
    harness.store.create(C.KIND_JOB, job)
    before = harness.store.resource_version
    harness.operator.run_once()
    assert harness.store.resource_version == before
    harness.operator.run_once()
    assert harness.store.resource_version == before


def test_gcs_claim_name_is_mounted():
    harness = Harness(feature_gates={"GCSFaultToleranceEmbeddedStorage": True})
    spec = cluster_spec(workers=0)
    spec["gcsFaultToleranceOptions"] = {"backend": "rocksdb", "storage": {"claimName": "my-existing-pvc"}}
    harness.apply(C.KIND_RAYCLUSTER, "byo-pvc", spec)
    harness.settle(30)
    head = harness.get(C.KIND_POD, "byo-pvc-head")
    volumes = {volume["name"]: volume for volume in head["spec"]["volumes"]}
    assert volumes["gcs-storage"]["persistentVolumeClaim"]["claimName"] == "my-existing-pvc"
    assert not harness.store.exists(C.KIND_PVC, "byo-pvc-gcs-pvc", "default")


def test_null_deletion_strategy_does_not_crash():
    harness = Harness()
    spec = {
        "entrypoint": "echo",
        "deletionStrategy": None,
        "rayClusterSpec": cluster_spec(workers=0),
    }
    harness.apply(C.KIND_RAYJOB, "null-policy", spec)
    harness.step(50, 0.5)
    job = harness.get(C.KIND_RAYJOB, "null-policy")
    assert job["status"]["jobDeploymentStatus"] == "Complete"
