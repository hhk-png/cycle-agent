#!/usr/bin/env python3
"""Maintenance lab: what a spec change actually does to running Pods.

    python examples/maintenance_lab.py

The lab answers the questions an on-call engineer asks before touching a
production RayCluster:

* Does changing the image restart every Pod, or only the next one created?
* Does a replica change trigger the same rollout as an image change?
* What does ``upgradeStrategy: Recreate`` change about that?
* How do I hold a provisioned head Pod down for maintenance?

The answers come from the operator's spec hash, not from guessing.  The lab
asserts Pod UIDs and images so "nothing happened" and "everything was
recreated" cannot be confused with each other.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from minikuberay import constants as C
from minikuberay.clock import ManualClock
from minikuberay.controllers.base import Operator, OperatorConfig
from minikuberay.models.meta import new_object


def cluster_spec(workers: int = 2, image: str = "rayproject/ray:2.59.0") -> dict:
    return {
        "rayVersion": "2.59.0",
        "headGroupSpec": {
            "rayStartParams": {},
            "template": {
                "spec": {
                    "restartPolicy": "Never",
                    "containers": [
                        {
                            "name": "ray",
                            "image": image,
                            "resources": {"requests": {"cpu": "1", "memory": "1Gi"}},
                        }
                    ],
                }
            },
        },
        "workerGroupSpecs": [
            {
                "groupName": "workers",
                "replicas": workers,
                "minReplicas": 0,
                "maxReplicas": 8,
                "rayStartParams": {},
                "template": {
                    "spec": {
                        "restartPolicy": "Never",
                        "containers": [
                            {
                                "name": "ray",
                                "image": image,
                                "resources": {"requests": {"cpu": "1", "memory": "1Gi"}},
                            }
                        ],
                    }
                },
            }
        ],
    }


def pods(operator: Operator) -> list[dict]:
    return operator.store.list(C.KIND_POD, "default", label_selector={C.LABEL_CLUSTER: "maintenance"})


def worker_pods(operator: Operator) -> list[dict]:
    return [
        pod
        for pod in pods(operator)
        if (pod["metadata"].get("labels") or {}).get(C.LABEL_NODE_TYPE) == C.NODE_TYPE_WORKER
    ]


def head_pods(operator: Operator) -> list[dict]:
    return [
        pod
        for pod in pods(operator)
        if (pod["metadata"].get("labels") or {}).get(C.LABEL_NODE_TYPE) == C.NODE_TYPE_HEAD
    ]


def uids(items: list[dict]) -> dict[str, str]:
    return {item["metadata"]["name"]: item["metadata"].get("uid", "") for item in items}


def images(items: list[dict]) -> set[str]:
    return {
        container.get("image", "")
        for item in items
        for container in ((item.get("spec") or {}).get("containers") or [])
        if container.get("name") == "ray"
    }


def settle(operator: Operator, clock: ManualClock, rounds: int = 40) -> None:
    for _ in range(rounds):
        operator.run_once()
        clock.advance(0.5)


def current(operator: Operator) -> dict:
    return operator.store.get(C.KIND_RAYCLUSTER, "maintenance", "default")


def main() -> int:
    clock = ManualClock()
    operator = Operator(clock=clock, config=OperatorConfig())
    failures = 0

    operator.store.create(
        C.KIND_RAYCLUSTER,
        new_object(C.RAY_API_VERSION, C.KIND_RAYCLUSTER, "maintenance", "default", spec=cluster_spec(2)),
        "default",
    )
    settle(operator, clock)
    baseline = uids(pods(operator))
    print(f"baseline        pods={len(baseline)} images={sorted(images(pods(operator)))}")
    if len(baseline) != 3 or images(pods(operator)) != {"rayproject/ray:2.59.0"}:
        print("  expected head + 2 workers on 2.59.0")
        failures += 1

    # ------------------------------------------------ image change, strategy None
    cluster = current(operator)
    cluster["spec"]["headGroupSpec"]["template"]["spec"]["containers"][0]["image"] = "rayproject/ray:2.60.0"
    cluster["spec"]["workerGroupSpecs"][0]["template"]["spec"]["containers"][0]["image"] = "rayproject/ray:2.60.0"
    operator.store.update(C.KIND_RAYCLUSTER, cluster)
    settle(operator, clock)
    after_none = uids(pods(operator))
    print(f"image/None      pods={len(after_none)} images={sorted(images(pods(operator)))} changed={sum(1 for name, uid in after_none.items() if baseline.get(name) != uid)}")
    if after_none != baseline:
        print("  expected upgradeStrategy=None to leave existing Pods untouched")
        failures += 1
    if images(pods(operator)) != {"rayproject/ray:2.59.0"}:
        print("  expected existing Pods to keep the old image")
        failures += 1

    # A new replica is built from the *new* template, producing a mixed cluster.
    cluster = current(operator)
    cluster["spec"]["workerGroupSpecs"][0]["replicas"] = 3
    operator.store.update(C.KIND_RAYCLUSTER, cluster)
    settle(operator, clock)
    mixed = images(worker_pods(operator))
    print(f"scale +1/None   workers={len(worker_pods(operator))} images={sorted(mixed)}")
    if len(worker_pods(operator)) != 3:
        print("  expected 3 workers")
        failures += 1
    if mixed != {"rayproject/ray:2.59.0", "rayproject/ray:2.60.0"}:
        print("  expected a mixed-version cluster: old Pods kept, the new Pod uses the new template")
        failures += 1

    # ------------------------------------------------------ Recreate: full rollout
    cluster = current(operator)
    cluster["spec"]["upgradeStrategy"] = {"type": "Recreate"}
    operator.store.update(C.KIND_RAYCLUSTER, cluster)
    settle(operator, clock)
    recreated = uids(pods(operator))
    print(f"recreate        pods={len(recreated)} images={sorted(images(pods(operator)))} replaced={sum(1 for name, uid in recreated.items() if baseline.get(name) != uid)}")
    if len(recreated) != 4 or images(pods(operator)) != {"rayproject/ray:2.60.0"}:
        print("  expected every Pod to run 2.60.0 after Recreate")
        failures += 1
    if any(recreated.get(name) == uid for name, uid in baseline.items() if name in recreated):
        print("  expected all original Pod UIDs to be replaced")
        failures += 1

    # --------------------------------------- a replica change is not a spec change
    cluster = current(operator)
    cluster["spec"]["workerGroupSpecs"][0]["replicas"] = 4
    before_replica_change = uids(pods(operator))
    operator.store.update(C.KIND_RAYCLUSTER, cluster)
    settle(operator, clock)
    after_replica_change = uids(pods(operator))
    kept = sum(1 for name, uid in before_replica_change.items() if after_replica_change.get(name) == uid)
    print(f"scale under Recreate workers={len(worker_pods(operator))} kept={kept}/{len(before_replica_change)}")
    if len(worker_pods(operator)) != 4 or kept != len(before_replica_change):
        print("  expected the replica change to add a Pod without recreating existing ones")
        failures += 1

    # ------------------------------------- hold the provisioned head down for work
    cluster = current(operator)
    cluster.setdefault("metadata", {}).setdefault("annotations", {})[C.ANNOTATION_DISABLE_HEAD_RESTART] = "true"
    operator.store.update(C.KIND_RAYCLUSTER, cluster)
    old_head = head_pods(operator)[0]
    operator.store.delete(C.KIND_POD, old_head["metadata"]["name"], "default", force=True)
    settle(operator, clock)
    print(f"head held       heads={len(head_pods(operator))} workers={len(worker_pods(operator))}")
    if head_pods(operator):
        print("  expected ray.io/disable-provisioned-head-restart to suppress head recreation")
        failures += 1
    if len(worker_pods(operator)) != 4:
        print("  expected workers to stay while the head is held down")
        failures += 1

    # Releasing the annotation lets the next reconcile restore the head.
    cluster = current(operator)
    cluster["metadata"]["annotations"].pop(C.ANNOTATION_DISABLE_HEAD_RESTART, None)
    operator.store.update(C.KIND_RAYCLUSTER, cluster)
    settle(operator, clock)
    print(f"head restored   heads={len(head_pods(operator))} workers={len(worker_pods(operator))}")
    if len(head_pods(operator)) != 1:
        print("  expected the head Pod to be recreated after the annotation was removed")
        failures += 1

    if failures:
        print(f"\nFAILED: {failures} check(s)")
        return 1
    print("\nOK: upgrade strategy, spec-hash scope, mixed-version risk and head maintenance are observable")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
