"""In-Place Pod Resizing (IPPR) contract tests.

These tests pin the parts that are easy to get wrong:

* the annotation is validated before the autoscaler ever acts on it;
* a resize keeps the Pod UID (no recreate) and updates both the Pod spec and
  the simulated Raylet's logical resources;
* demand beyond ``max-cpu`` falls back to horizontal scale-out;
* a failed resize rolls back and the demand is served horizontally.
"""

from __future__ import annotations

import copy
import json

from minikuberay import constants as C
from minikuberay.models.defaults import apply_defaults
from minikuberay.models.ippr import parse_ippr, pod_resources, status_of
from minikuberay.models.meta import new_object
from minikuberay.models.validation import validate_object
from tests.conftest import Harness, cluster_spec


def ippr_spec(*, workers: int = 1, max_cpu: str = "4", max_memory: str = "4Gi", result: str | None = None) -> dict:
    spec = cluster_spec(
        workers=workers,
        min_replicas=1,
        max_replicas=10,
        autoscaling=True,
        worker_cpu="1",
        worker_memory="1Gi",
        group_name="small",
    )
    spec["autoscalerOptions"] = {"version": "v2", "upscalingMode": "Default", "idleTimeoutSeconds": 15}
    spec["headGroupSpec"]["rayStartParams"] = {"num-cpus": "0"}
    group = spec["workerGroupSpecs"][0]
    container = group["template"]["spec"]["containers"][0]
    container["resources"]["limits"] = {"cpu": max_cpu, "memory": max_memory}
    container["resizePolicy"] = [
        {"resourceName": "cpu", "restartPolicy": "NotRequired"},
        {"resourceName": "memory", "restartPolicy": "NotRequired"},
    ]
    if result:
        group["template"].setdefault("metadata", {}).setdefault("annotations", {})[
            C.ANNOTATION_SIM_IPPR_RESULT
        ] = result
    return spec


def ippr_annotation(max_cpu: str = "4", max_memory: str = "4Gi", timeout: int = 60) -> dict:
    return {
        C.ANNOTATION_IPPR: json.dumps(
            {"groups": {"small": {"max-cpu": max_cpu, "max-memory": max_memory, "resize-timeout": timeout}}}
        )
    }


def apply_cluster(harness: Harness, *, name: str = "ippr", spec: dict | None = None, max_cpu: str = "4") -> dict:
    return harness.apply(
        C.KIND_RAYCLUSTER,
        name,
        copy.deepcopy(spec or ippr_spec()),
        annotations=ippr_annotation(max_cpu=max_cpu),
    )


def workers(harness: Harness, name: str = "ippr") -> list[dict]:
    return [
        pod
        for pod in harness.list(C.KIND_POD)
        if (pod["metadata"]["labels"].get(C.LABEL_CLUSTER) == name)
        and pod["metadata"]["labels"].get(C.LABEL_NODE_TYPE) == C.NODE_TYPE_WORKER
    ]


def cluster_spec_of(harness: Harness, name: str = "ippr") -> dict:
    return harness.get(C.KIND_RAYCLUSTER, name)


# --------------------------------------------------------------------- parsing
def test_parse_ippr_accepts_valid_configuration():
    obj = apply_defaults(new_object(C.RAY_API_VERSION, C.KIND_RAYCLUSTER, "ippr", "default", spec=ippr_spec()))
    obj["metadata"]["annotations"].update(ippr_annotation())
    configs, errors = parse_ippr(obj)
    assert errors == []
    assert configs["small"].max_cpu == 4000
    assert configs["small"].max_memory == 4 * 2**30
    assert configs["small"].timeout_seconds == 60


def test_parse_ippr_rejects_ray_start_params_resources():
    spec = ippr_spec()
    spec["workerGroupSpecs"][0]["rayStartParams"] = {"num-cpus": "2"}
    obj = apply_defaults(new_object(C.RAY_API_VERSION, C.KIND_RAYCLUSTER, "ippr", "default", spec=spec))
    obj["metadata"]["annotations"].update(ippr_annotation())
    _, errors = parse_ippr(obj)
    assert any("num-cpus" in error for error in errors)


def test_parse_ippr_rejects_restarting_resize_policy():
    spec = ippr_spec()
    spec["workerGroupSpecs"][0]["template"]["spec"]["containers"][0]["resizePolicy"] = [
        {"resourceName": "cpu", "restartPolicy": "RestartContainer"}
    ]
    obj = apply_defaults(new_object(C.RAY_API_VERSION, C.KIND_RAYCLUSTER, "ippr", "default", spec=spec))
    obj["metadata"]["annotations"].update(ippr_annotation())
    _, errors = parse_ippr(obj)
    assert any("NotRequired" in error for error in errors)


def test_ippr_requires_autoscaler_v2():
    spec = ippr_spec()
    spec["autoscalerOptions"]["version"] = "v1"
    obj = apply_defaults(new_object(C.RAY_API_VERSION, C.KIND_RAYCLUSTER, "ippr", "default", spec=spec))
    obj["metadata"]["annotations"].update(ippr_annotation())
    _, errors = parse_ippr(obj)
    assert any("v2" in error for error in errors)


def test_validation_surfaces_unknown_group():
    obj = apply_defaults(new_object(C.RAY_API_VERSION, C.KIND_RAYCLUSTER, "ippr", "default", spec=ippr_spec()))
    obj["metadata"]["annotations"][C.ANNOTATION_IPPR] = json.dumps(
        {"groups": {"missing": {"max-cpu": "4", "max-memory": "4Gi", "resize-timeout": 60}}}
    )
    errors = validate_object(obj)
    assert any("does not match" in error for error in errors)


# ----------------------------------------------------------------- controller
def test_ippr_resizes_in_place_without_recreating_the_pod(harness):
    apply_cluster(harness)
    harness.settle(40, advance=0.5)
    before = workers(harness)
    assert len(before) == 1
    uid_before = before[0]["metadata"]["uid"]

    harness.runtime.add_demand("default", "ippr", cpu=3, count=1, ttl_seconds=300)
    harness.settle(40, advance=0.5)

    after = workers(harness)
    assert len(after) == 1
    assert after[0]["metadata"]["uid"] == uid_before  # no recreate
    assert pod_resources(after[0]).cpu == 3000
    assert status_of(after[0])["state"] == "succeeded"
    assert cluster_spec_of(harness)["spec"]["workerGroupSpecs"][0]["replicas"] == 1
    node = next(node for node in harness.runtime.nodes("default", "ippr") if node.node_type == C.NODE_TYPE_WORKER)
    assert node.cpu == 3000


def test_ippr_falls_back_to_horizontal_when_max_is_reached(harness):
    apply_cluster(harness)
    harness.settle(40, advance=0.5)
    harness.runtime.add_demand("default", "ippr", cpu=6, count=1, ttl_seconds=300)
    harness.settle(80, advance=0.5)

    pods = workers(harness)
    # 4 CPU in the resized Pod + two 1-CPU Pods == 6 CPU.
    assert len(pods) == 3
    assert cluster_spec_of(harness)["spec"]["workerGroupSpecs"][0]["replicas"] == 3
    resized = sorted(pod_resources(pod).cpu for pod in pods)
    assert resized == [1000, 1000, 4000]


def test_ippr_timeout_rolls_back_and_serves_demand_horizontally(harness):
    apply_cluster(harness, spec=ippr_spec(result="timeout"))
    harness.settle(40, advance=0.5)
    harness.runtime.add_demand("default", "ippr", cpu=2, count=1, ttl_seconds=300)
    harness.settle(80, advance=0.5)

    pods = sorted(workers(harness), key=lambda pod: pod["metadata"]["name"])
    # The original Pod is back at 1 CPU; a second Pod provides the rest.
    assert pod_resources(pods[0]).cpu == 1000
    assert status_of(pods[0])["state"] == "rolled-back"
    assert len(pods) == 2
    assert cluster_spec_of(harness)["spec"]["workerGroupSpecs"][0]["replicas"] == 2


def test_ippr_resizes_memory_only_demand(harness):
    apply_cluster(harness)
    harness.settle(40, advance=0.5)
    harness.runtime.add_demand("default", "ippr", memory_mb=2048, count=1, ttl_seconds=300)
    harness.settle(40, advance=0.5)

    pods = workers(harness)
    assert len(pods) == 1
    assert pod_resources(pods[0]).cpu == 1000
    assert pod_resources(pods[0]).memory == 2 * 2**30
    assert status_of(pods[0])["state"] == "succeeded"
    assert cluster_spec_of(harness)["spec"]["workerGroupSpecs"][0]["replicas"] == 1


def test_ippr_multihost_uses_per_pod_capacity(harness):
    spec = ippr_spec(workers=2)
    spec["workerGroupSpecs"][0]["numOfHosts"] = 2
    apply_cluster(harness, spec=spec)
    harness.settle(40, advance=0.5)
    assert len(workers(harness)) == 4
    harness.runtime.add_demand("default", "ippr", cpu=6, count=1, ttl_seconds=300)
    harness.settle(80, advance=0.5)

    pods = workers(harness)
    # 4 Pods at 1 CPU already provide 4 CPU; one grows to 3 and the rest stay
    # at 1, for exactly 6 CPU.  Replicas stay at 2 (each has 2 hosts).
    assert len(pods) == 4
    assert sorted(pod_resources(pod).cpu for pod in pods) == [1000, 1000, 1000, 3000]
    assert cluster_spec_of(harness)["spec"]["workerGroupSpecs"][0]["replicas"] == 2


def test_ippr_rejects_boolean_and_fractional_timeout():
    obj = apply_defaults(new_object(C.RAY_API_VERSION, C.KIND_RAYCLUSTER, "ippr", "default", spec=ippr_spec()))
    obj["metadata"]["annotations"][C.ANNOTATION_IPPR] = json.dumps(
        {"groups": {"small": {"max-cpu": True, "max-memory": "4Gi", "resize-timeout": 60}}}
    )
    _, errors = parse_ippr(obj)
    assert any("boolean" in error for error in errors)

    obj["metadata"]["annotations"][C.ANNOTATION_IPPR] = json.dumps(
        {"groups": {"small": {"max-cpu": "4", "max-memory": "4Gi", "resize-timeout": 60.9}}}
    )
    _, errors = parse_ippr(obj)
    assert any("integer" in error for error in errors)

    obj["metadata"]["annotations"][C.ANNOTATION_IPPR] = json.dumps(
        {"groups": {"small": {"max-cpu": "4", "max-memory": "4Gi", "resize-timeout": 60, "max-gpu": "1"}}}
    )
    _, errors = parse_ippr(obj)
    assert any("unknown" in error for error in errors)


def test_complete_resize_preserves_gpu_limits_and_removes_condition():
    from minikuberay.clock import ManualClock
    from minikuberay.models.ippr import complete_resize

    pod = {
        "metadata": {
            "name": "p",
            "annotations": {C.ANNOTATION_IPPR_STATUS: json.dumps({"state": "resizing"})},
        },
        "spec": {
            "containers": [
                {
                    "name": "ray",
                    "resources": {
                        "requests": {"cpu": "2", "memory": "2Gi", "nvidia.com/gpu": "1"},
                        "limits": {"cpu": "4", "memory": "4Gi", "nvidia.com/gpu": "1"},
                    },
                }
            ]
        },
        "status": {
            "containerStatuses": [{"name": "ray", "resources": {}}],
            "conditions": [{"type": "PodResizeInProgress", "status": "True"}],
        },
    }
    completed, changed = complete_resize(pod, clock=ManualClock())
    assert changed
    resources = completed["status"]["containerStatuses"][0]["resources"]
    assert resources["requests"]["nvidia.com/gpu"] == "1"
    assert resources["limits"]["cpu"] == "4"
    assert not any(item.get("type") == "PodResizeInProgress" for item in completed["status"]["conditions"])


def test_failed_resize_rolls_back_after_annotation_is_removed(harness):
    apply_cluster(harness, spec=ippr_spec(result="failure"))
    harness.settle(40, advance=0.5)
    harness.runtime.add_demand("default", "ippr", cpu=2, count=1, ttl_seconds=300)
    harness.step(1)
    # The resize request is now in flight, but the operator loses its IPPR
    # config.  The rollback must still happen (it is a data-plane repair).
    cluster = harness.get(C.KIND_RAYCLUSTER, "ippr")
    cluster["metadata"]["annotations"].pop(C.ANNOTATION_IPPR)
    harness.store.update(C.KIND_RAYCLUSTER, cluster)
    harness.runtime.clear_demand("default", "ippr")
    harness.settle(20, advance=1.0)

    pod = workers(harness)[0]
    assert pod_resources(pod).cpu == 1000
    assert status_of(pod)["state"] == "rolled-back"
    node = next(node for node in harness.runtime.nodes("default", "ippr") if node.node_type == C.NODE_TYPE_WORKER)
    assert node.cpu == 1000


def test_resize_timeout_is_enforced_by_the_kubelet(harness):
    spec = ippr_spec()
    spec["workerGroupSpecs"][0]["template"].setdefault("metadata", {}).setdefault("annotations", {})[
        C.ANNOTATION_SIM_IPPR_DELAY
    ] = "1000"
    apply_cluster(harness, spec=spec)
    harness.settle(40, advance=0.5)
    harness.runtime.add_demand("default", "ippr", cpu=2, count=1, ttl_seconds=300)
    # 60-second resize-timeout: advance well past it without waiting in real
    # time.  ``settle`` would stop as soon as a round makes no change, so use
    # an explicit step to keep the clock moving.
    harness.step(200, advance=0.5)

    pod = workers(harness)[0]
    assert status_of(pod)["state"] == "rolled-back"
    assert pod_resources(pod).cpu == 1000


def test_manual_scale_down_with_autoscaling_removes_surplus_pods(harness):
    spec = cluster_spec(workers=2, min_replicas=1, max_replicas=10, autoscaling=True, group_name="workers")
    harness.apply(C.KIND_RAYCLUSTER, "manual-down", copy.deepcopy(spec))
    harness.settle(40, advance=0.5)
    assert len(workers(harness, "manual-down")) == 2

    cluster = harness.get(C.KIND_RAYCLUSTER, "manual-down")
    cluster["spec"]["workerGroupSpecs"][0]["replicas"] = 1
    harness.store.update(C.KIND_RAYCLUSTER, cluster)
    # Keep advancing past idleTimeoutSeconds; settle would stop at the first
    # no-op round, which is before the idle timer expires.
    harness.step(80, advance=1.0)
    assert len(workers(harness, "manual-down")) == 1


def test_memory_demand_is_not_double_counted_across_groups(harness):
    def group(name: str, memory: str) -> dict:
        return {
            "groupName": name,
            "replicas": 1,
            "minReplicas": 1,
            "maxReplicas": 5,
            "rayStartParams": {},
            "template": {
                "spec": {
                    "restartPolicy": "Never",
                    "containers": [
                        {
                            "name": "ray",
                            "image": "rayproject/ray:2.59.0",
                            "resources": {"requests": {"cpu": "1", "memory": memory}},
                        }
                    ],
                }
            },
        }

    spec = cluster_spec(workers=1, min_replicas=1, max_replicas=5, autoscaling=True, group_name="a")
    spec["workerGroupSpecs"] = [group("a", "2Gi"), group("b", "1Gi")]
    harness.apply(C.KIND_RAYCLUSTER, "multi-memory", copy.deepcopy(spec))
    harness.settle(40, advance=0.5)
    harness.runtime.add_demand("default", "multi-memory", memory_mb=3072, count=1, ttl_seconds=300)
    harness.settle(40, advance=0.5)

    cluster = harness.get(C.KIND_RAYCLUSTER, "multi-memory")
    groups = {item["groupName"]: item["replicas"] for item in cluster["spec"]["workerGroupSpecs"]}
    # 3Gi of demand: one extra 2Gi replica in group a satisfies it; group b
    # must not add another replica for the same demand.
    assert groups == {"a": 2, "b": 1}


def test_rayjob_forwards_ippr_to_the_child_cluster(harness):
    spec = {
        "entrypoint": "python -c \"print('x')\"",
        "submissionMode": "K8sJobMode",
        "shutdownAfterJobFinishes": False,
        "rayClusterSpec": ippr_spec(workers=1),
    }
    harness.apply(C.KIND_RAYJOB, "job-ippr", copy.deepcopy(spec), annotations=ippr_annotation())
    harness.step(10, advance=0.5)
    clusters = harness.list(C.KIND_RAYCLUSTER)
    assert clusters, "RayJob should create a child RayCluster"
    assert (clusters[0]["metadata"].get("annotations") or {}).get(C.ANNOTATION_IPPR)


def test_rayservice_forwards_ippr_to_the_child_cluster(harness):
    serve = """applications:
  - name: app
    import_path: app:main
    route_prefix: /
    deployments:
      - name: D
        num_replicas: 1
"""
    spec = {
        "serveConfigV2": serve,
        "rayClusterConfig": ippr_spec(workers=1),
    }
    harness.apply(C.KIND_RAYSERVICE, "svc-ippr", copy.deepcopy(spec), annotations=ippr_annotation())
    harness.step(10, advance=0.2)
    clusters = harness.list(C.KIND_RAYCLUSTER)
    assert clusters, "RayService should create an active RayCluster"
    assert (clusters[0]["metadata"].get("annotations") or {}).get(C.ANNOTATION_IPPR)


def test_ippr_is_idempotent_at_convergence(harness):
    apply_cluster(harness)
    harness.settle(40, advance=0.5)
    harness.runtime.add_demand("default", "ippr", cpu=2, count=1, ttl_seconds=300)
    harness.settle(40, advance=0.5)
    # A converged operator must not keep rewriting the resized Pod.
    pod = workers(harness)[0]
    version = pod["metadata"]["resourceVersion"]
    harness.step(5)
    assert workers(harness)[0]["metadata"]["resourceVersion"] == version
