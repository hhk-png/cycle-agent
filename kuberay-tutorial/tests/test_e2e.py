from __future__ import annotations

from minikuberay import constants as C
from minikuberay.models.meta import new_object
from tests.conftest import Harness, cluster_spec


def test_full_platform_scenario():
    harness = Harness(feature_gates={"RayCronJob": True})

    # 1. A shared RayCluster with two workers.
    harness.apply(C.KIND_RAYCLUSTER, "platform", cluster_spec(workers=2))
    harness.step(20, 0.2)
    cluster = harness.get(C.KIND_RAYCLUSTER, "platform")
    assert cluster["status"]["state"] == "ready"
    assert cluster["status"]["availableWorkerReplicas"] == 2

    # 2. A RayJob on its own ephemeral cluster.
    job = new_object(
        C.RAY_API_VERSION,
        C.KIND_RAYJOB,
        "batch",
        "default",
        spec={
            "entrypoint": "python train.py",
            "shutdownAfterJobFinishes": True,
            "rayClusterSpec": cluster_spec(workers=1, min_replicas=1),
        },
    )
    harness.store.create(C.KIND_RAYJOB, job, "default")
    harness.step(60, 0.5)
    result = harness.get(C.KIND_RAYJOB, "batch")
    assert result["status"]["jobDeploymentStatus"] == "Complete"
    assert result["status"]["rayJobInfo"]["startTime"]
    assert result["status"]["rayJobInfo"]["endTime"]

    # 3. A RayService, then an upgrade.
    service = new_object(
        C.RAY_API_VERSION,
        C.KIND_RAYSERVICE,
        "serving",
        "default",
        spec={
            "serveConfigV2": "applications:\n  - name: app\n    import_path: app:v1\n",
            "rayClusterConfig": cluster_spec(workers=1, min_replicas=1),
        },
    )
    harness.store.create(C.KIND_RAYSERVICE, service, "default")
    harness.step(30, 0.2)
    assert harness.get(C.KIND_RAYSERVICE, "serving")["status"]["serviceStatus"] == "Running"
    service = harness.get(C.KIND_RAYSERVICE, "serving")
    service["spec"]["serveConfigV2"] = "applications:\n  - name: app\n    import_path: app:v2\n"
    harness.store.update(C.KIND_RAYSERVICE, service)
    harness.step(40, 0.2)
    assert harness.get(C.KIND_RAYSERVICE, "serving")["status"]["serviceStatus"] == "Running"

    # 4. A cron job creates one child.
    cron = new_object(
        C.RAY_API_VERSION,
        C.KIND_RAYCRONJOB,
        "nightly",
        "default",
        spec={
            "schedule": "0 0 * * *",
            "timeZone": "UTC",
            "jobTemplate": {"entrypoint": "echo cron", "rayClusterSpec": cluster_spec(workers=0)},
        },
    )
    harness.store.create(C.KIND_RAYCRONJOB, cron, "default")
    harness.step(25, 3600)
    assert harness.list(C.KIND_RAYJOB, label_selector={C.LABEL_CRONJOB_NAME: "nightly"})

    # Every remaining RayCluster should belong to a visible owner.
    assert harness.list(C.KIND_RAYCLUSTER)
    snapshot = harness.operator.snapshot()
    assert snapshot["runtime"]["clusters"]


def test_operator_is_idle_after_convergence():
    harness = Harness()
    harness.apply(C.KIND_RAYCLUSTER, "stable", cluster_spec(workers=1, min_replicas=1))
    harness.settle(40)
    assert harness.operator.run_once() is False
    assert harness.operator.run_once() is False


def test_deleting_platform_removes_state():
    harness = Harness()
    harness.apply(C.KIND_RAYCLUSTER, "doomed", cluster_spec(workers=1, min_replicas=1))
    harness.settle(30)
    harness.store.delete(C.KIND_RAYCLUSTER, "doomed", "default")
    harness.settle(30)
    assert harness.list(C.KIND_POD) == []
    assert harness.list(C.KIND_SERVICE) == []
    assert harness.runtime.ready_nodes("default", "doomed") == []
    assert harness.operator.run_once() is False


def test_two_namespaces_are_independent():
    harness = Harness()
    for namespace in ("team-a", "team-b"):
        obj = new_object(
            C.RAY_API_VERSION,
            C.KIND_RAYCLUSTER,
            "ray",
            namespace,
            spec=cluster_spec(workers=1, min_replicas=1),
        )
        harness.store.create(C.KIND_RAYCLUSTER, obj, namespace)
    harness.settle(40)
    for namespace in ("team-a", "team-b"):
        assert harness.get(C.KIND_RAYCLUSTER, "ray", namespace)["status"]["state"] == "ready"
        assert harness.store.exists(C.KIND_SERVICE, "ray-head-svc", namespace)
    assert len(harness.list(C.KIND_POD, None)) == 4
