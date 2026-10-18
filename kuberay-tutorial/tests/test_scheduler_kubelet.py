from __future__ import annotations

from minikuberay import constants as C
from minikuberay.models.core import new_pod, new_service, pod_is_ready, service_port


def pod(name="p", *, cpu="1", memory="1Gi", gpu=None, labels=None, annotations=None, node_selector=None):
    resources = {"requests": {"cpu": cpu, "memory": memory}}
    if gpu is not None:
        resources["requests"]["nvidia.com/gpu"] = str(gpu)
    spec = {
        "containers": [{"name": "ray", "image": "x", "resources": resources}],
        "restartPolicy": "Never",
    }
    if node_selector:
        spec["nodeSelector"] = node_selector
    return new_pod(name, "default", spec, labels=labels or {C.LABEL_CLUSTER: "c", C.LABEL_NODE_TYPE: C.NODE_TYPE_HEAD}, annotations=annotations)


def test_scheduler_assigns_and_kubelet_runs(harness):
    created = harness.store.create(C.KIND_POD, pod())
    harness.step(3)
    current = harness.get(C.KIND_POD, "p")
    assert current["spec"]["nodeName"] in ("node-a", "node-b", "node-c")
    assert current["status"]["phase"] == "Running"
    assert current["status"]["podIP"].startswith("10.244.")
    assert pod_is_ready(current)
    assert [node.pod_name for node in harness.runtime.ready_nodes("default", "c")] == ["p"]


def test_scheduler_unschedulable(harness):
    harness.store.create(C.KIND_POD, pod(cpu="100"))
    harness.step(3)
    current = harness.get(C.KIND_POD, "p")
    assert current["status"]["phase"] == "Pending"
    condition = current["status"]["conditions"][0]
    assert condition["reason"] == "Unschedulable"


def test_scheduler_gpu_fit(harness):
    harness.store.create(C.KIND_POD, pod("gpu-pod", gpu=1))
    harness.step(3)
    current = harness.get(C.KIND_POD, "gpu-pod")
    assert current["status"]["phase"] == "Running"
    assert current["spec"]["nodeName"] in ("node-b", "node-c")


def test_scheduler_node_selector(harness):
    harness.store.create(C.KIND_POD, pod("selected", node_selector={"topology.kubernetes.io/zone": "c"}))
    harness.step(3)
    assert harness.get(C.KIND_POD, "selected")["spec"]["nodeName"] == "node-c"


def test_kubelet_crash_injection(harness):
    harness.store.create(C.KIND_POD, pod("crash", annotations={C.ANNOTATION_SIM_CRASH_AFTER: "1"}))
    harness.step(3)
    assert harness.get(C.KIND_POD, "crash")["status"]["phase"] == "Running"
    harness.clock.advance(2)
    harness.step(1)
    crashed = harness.get(C.KIND_POD, "crash")
    assert crashed["status"]["phase"] == "Failed"
    assert crashed["status"]["reason"] == "SimulatedCrash"


def test_kubelet_deletes_terminating_pod(harness):
    harness.store.create(C.KIND_POD, pod())
    harness.step(3)
    harness.store.delete(C.KIND_POD, "p", "default")
    harness.step(1)
    assert not harness.store.exists(C.KIND_POD, "p", "default")


def test_runtime_unregister_on_crash(harness):
    harness.store.create(C.KIND_POD, pod("crash", annotations={C.ANNOTATION_SIM_CRASH_AFTER: "1"}))
    harness.step(3)
    assert len(harness.runtime.ready_nodes("default", "c")) == 1
    harness.clock.advance(2)
    harness.step(1)
    assert harness.runtime.ready_nodes("default", "c") == []


def test_endpoint_slice_controller(harness):
    service = new_service("svc", "default", {C.LABEL_CLUSTER: "c"}, [service_port("client", 10001)])
    harness.store.create(C.KIND_SERVICE, service)
    harness.store.create(C.KIND_POD, pod("head"))
    harness.step(3)
    harness.endpoints.reconcile_all()
    slices = harness.list(C.KIND_ENDPOINTSLICE)
    assert len(slices) == 1
    assert len(slices[0]["endpoints"]) == 1
    assert slices[0]["endpoints"][0]["addresses"][0] == harness.get(C.KIND_POD, "head")["status"]["podIP"]


def test_endpoint_slice_empty_without_pods(harness):
    harness.store.create(C.KIND_SERVICE, new_service("empty", "default", {C.LABEL_CLUSTER: "none"}, [service_port("client", 1)]))
    harness.endpoints.reconcile_all()
    slices = harness.list(C.KIND_ENDPOINTSLICE)
    assert slices[0]["endpoints"] == []


def test_toleration_value_and_operator_are_honored():
    from minikuberay.controllers.batch import SimulatedScheduler
    from minikuberay.models.core import new_pod

    scheduler = SimulatedScheduler()
    scheduler.nodes[0].taints = [{"key": "dedicated", "value": "gpu", "effect": "NoSchedule"}]
    wrong = new_pod(
        "wrong",
        "default",
        {
            "nodeSelector": {"kubernetes.io/hostname": "node-a"},
            "tolerations": [{"key": "dedicated", "operator": "Equal", "value": "cpu", "effect": "NoSchedule"}],
            "containers": [{"name": "ray", "image": "x", "resources": {"requests": {"cpu": "100m"}}}],
        },
    )
    assert scheduler.assign(wrong, []) is None
    right = new_pod(
        "right",
        "default",
        {
            "nodeSelector": {"kubernetes.io/hostname": "node-a"},
            "tolerations": [{"key": "dedicated", "operator": "Equal", "value": "gpu", "effect": "NoSchedule"}],
            "containers": [{"name": "ray", "image": "x", "resources": {"requests": {"cpu": "100m"}}}],
        },
    )
    assert scheduler.assign(right, []) == "node-a"


def test_pod_with_finalizer_stays_terminating(harness):
    from minikuberay.models.core import new_pod

    pod = new_pod("stuck", "default", {"containers": [{"name": "ray", "image": "x"}]}, finalizers=["example.com/cleanup"])
    harness.store.create(C.KIND_POD, pod)
    harness.store.delete(C.KIND_POD, "stuck", "default")
    harness.step(5)
    current = harness.get(C.KIND_POD, "stuck")
    assert current["metadata"].get("deletionTimestamp")
    assert "example.com/cleanup" in current["metadata"]["finalizers"]
