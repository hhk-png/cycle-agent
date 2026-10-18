from __future__ import annotations

from minikuberay import constants as C
from minikuberay.controllers.base import OperatorConfig
from tests.conftest import Harness, cluster_spec


def harness_with(scheduler: str, *, gates: dict | None = None) -> Harness:
    config = OperatorConfig(batch_scheduler=scheduler)
    if gates:
        config.feature_gates.update(gates)
    return Harness(config=config)


def test_volcano_creates_podgroup_and_sets_scheduler_name():
    harness = harness_with("volcano")
    harness.apply(C.KIND_RAYCLUSTER, "gang", cluster_spec(workers=2, min_replicas=2))
    harness.settle(40)
    podgroup = harness.get(C.KIND_PODGROUP, "gang-podgroup")
    assert podgroup["apiVersion"] == "scheduling.volcano.sh/v1beta1"
    assert podgroup["spec"]["minMember"] == 3  # head + two workers
    for pod in harness.list(C.KIND_POD):
        assert pod["spec"]["schedulerName"] == "volcano"


def test_yunikorn_uses_scheduler_name_without_podgroup():
    harness = harness_with("yunikorn")
    harness.apply(C.KIND_RAYCLUSTER, "yk", cluster_spec(workers=1, min_replicas=1))
    harness.settle(30)
    assert not harness.store.exists(C.KIND_PODGROUP, "yk-podgroup", "default")
    for pod in harness.list(C.KIND_POD):
        assert pod["spec"]["schedulerName"] == "yunikorn"


def test_scheduler_plugins_podgroup_api_version():
    harness = harness_with("scheduler-plugins")
    harness.apply(C.KIND_RAYCLUSTER, "cosched", cluster_spec(workers=1, min_replicas=1))
    harness.settle(30)
    podgroup = harness.get(C.KIND_PODGROUP, "cosched-podgroup")
    assert podgroup["apiVersion"] == "scheduling.x-k8s.io/v1alpha1"
    assert podgroup["spec"]["schedulerName"] == "scheduler-plugins-scheduler"


def test_disabling_batch_scheduler_prunes_podgroup():
    harness = harness_with("volcano")
    harness.apply(C.KIND_RAYCLUSTER, "toggle", cluster_spec(workers=1, min_replicas=1))
    harness.settle(30)
    assert harness.store.exists(C.KIND_PODGROUP, "toggle-podgroup", "default")
    harness.config.batch_scheduler = ""
    harness.settle(30)
    assert not harness.store.exists(C.KIND_PODGROUP, "toggle-podgroup", "default")
    # Existing Pods keep the schedulerName they were created with; changing an
    # operator-level flag is not a Pod template change.  A rollout (or Pod
    # deletion) is required to apply the new scheduler, exactly as with the
    # real operator.
    assert all(pod["spec"]["schedulerName"] == "volcano" for pod in harness.list(C.KIND_POD))


def test_was_requires_feature_gate():
    harness = harness_with("was")
    harness.apply(C.KIND_RAYCLUSTER, "was", cluster_spec(workers=0))
    harness.settle(20)
    cluster = harness.get(C.KIND_RAYCLUSTER, "was")
    assert cluster["status"]["reason"] == "ValidationFailed"

    harness.config.feature_gates["KubernetesWAS"] = True
    harness.settle(30)
    assert harness.get(C.KIND_RAYCLUSTER, "was")["status"]["state"] == "ready"
