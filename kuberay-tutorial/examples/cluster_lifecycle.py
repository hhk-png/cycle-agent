#!/usr/bin/env python3
"""RayCluster lifecycle lab: suspend, resume, Recreate upgrade and scale.

    python examples/cluster_lifecycle.py

These are the operations that page an on-call engineer most often, and each one
has a different failure mode:

* suspend must delete Pods but keep the CR (and its identity);
* resume must converge back to the same worker groups;
* a Recreate upgrade must replace *all* Pods, not just the head;
* scaleStrategy.workersToDelete must remove exactly the named Pods and then
  clear itself, otherwise the next reconcile deletes the replacement too.

The lab asserts Pod UIDs, not just counts, so "delete and recreate" cannot be
confused with "nothing happened".
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from minikuberay import constants as C
from minikuberay.clock import ManualClock
from minikuberay.controllers.base import Operator, OperatorConfig
from minikuberay.models.meta import new_object


def cluster_spec(workers: int = 2) -> dict:
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
                            "image": "rayproject/ray:2.59.0",
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
                "maxReplicas": 6,
                "rayStartParams": {},
                "template": {
                    "spec": {
                        "restartPolicy": "Never",
                        "containers": [
                            {
                                "name": "ray",
                                "image": "rayproject/ray:2.59.0",
                                "resources": {"requests": {"cpu": "1", "memory": "1Gi"}},
                            }
                        ],
                    }
                },
            }
        ],
    }


def pods(operator: Operator) -> list[dict]:
    return operator.store.list(C.KIND_POD, "default", label_selector={C.LABEL_CLUSTER: "lifecycle"})


def worker_pods(operator: Operator) -> list[dict]:
    return [
        pod
        for pod in pods(operator)
        if (pod["metadata"].get("labels") or {}).get("ray.io/node-type") == "worker"
    ]


def uids(items: list[dict]) -> dict[str, str]:
    return {item["metadata"]["name"]: item["metadata"].get("uid", "") for item in items}


def settle(operator: Operator, clock: ManualClock, rounds: int = 40) -> None:
    for _ in range(rounds):
        operator.run_once()
        clock.advance(0.5)


def main() -> int:
    clock = ManualClock()
    operator = Operator(clock=clock, config=OperatorConfig())
    failures = 0

    operator.store.create(C.KIND_RAYCLUSTER, new_object(C.RAY_API_VERSION, C.KIND_RAYCLUSTER, "lifecycle", "default", spec=cluster_spec(2)), "default")
    settle(operator, clock)
    before = uids(pods(operator))
    state = operator.store.get(C.KIND_RAYCLUSTER, "lifecycle", "default").get("status", {}).get("state")
    print(f"created       pods={len(before)} state={state}")
    if len(before) != 3 or state != "ready":
        print("  expected head + 2 workers and state=ready")
        failures += 1

    # --------------------------------------------------------------- suspend
    cluster = operator.store.get(C.KIND_RAYCLUSTER, "lifecycle", "default")
    cluster["spec"]["suspend"] = True
    operator.store.update(C.KIND_RAYCLUSTER, cluster)
    settle(operator, clock)
    suspended = operator.store.get(C.KIND_RAYCLUSTER, "lifecycle", "default")
    print(f"suspend       pods={len(pods(operator))} state={suspended.get('status', {}).get('state')}")
    if pods(operator):
        print("  expected suspend to delete all Pods")
        failures += 1
    if suspended.get("status", {}).get("state") != "suspended":
        print("  expected state=suspended")
        failures += 1
    if not operator.store.exists(C.KIND_RAYCLUSTER, "lifecycle", "default"):
        print("  expected the RayCluster CR to survive suspend")
        failures += 1

    # ---------------------------------------------------------------- resume
    cluster = operator.store.get(C.KIND_RAYCLUSTER, "lifecycle", "default")
    cluster["spec"]["suspend"] = False
    operator.store.update(C.KIND_RAYCLUSTER, cluster)
    settle(operator, clock)
    resumed = uids(pods(operator))
    print(f"resume        pods={len(resumed)} state={operator.store.get(C.KIND_RAYCLUSTER, 'lifecycle', 'default').get('status', {}).get('state')}")
    if len(resumed) != 3:
        print("  expected head + 2 workers after resume")
        failures += 1
    if set(resumed.values()) & set(before.values()):
        print("  expected new Pod UIDs after resume")
        failures += 1

    # ------------------------------------------------------- Recreate upgrade
    cluster = operator.store.get(C.KIND_RAYCLUSTER, "lifecycle", "default")
    cluster["spec"]["upgradeStrategy"] = {"type": "Recreate"}
    cluster["spec"]["headGroupSpec"]["template"]["spec"]["containers"][0]["image"] = "rayproject/ray:2.60.0"
    cluster["spec"]["workerGroupSpecs"][0]["template"]["spec"]["containers"][0]["image"] = "rayproject/ray:2.60.0"
    operator.store.update(C.KIND_RAYCLUSTER, cluster)
    settle(operator, clock)
    upgraded = uids(pods(operator))
    images = {
        pod["spec"]["containers"][0].get("image")
        for pod in pods(operator)
    }
    print(f"recreate      pods={len(upgraded)} images={sorted(images)}")
    if len(upgraded) != 3 or images != {"rayproject/ray:2.60.0"}:
        print("  expected every Pod to be recreated with the new image")
        failures += 1

    # ------------------------------------------- scaleStrategy.workersToDelete
    victims = worker_pods(operator)
    victim_name = sorted(pod["metadata"]["name"] for pod in victims)[0]
    cluster = operator.store.get(C.KIND_RAYCLUSTER, "lifecycle", "default")
    cluster["spec"]["workerGroupSpecs"][0].setdefault("scaleStrategy", {})["workersToDelete"] = [victim_name]
    operator.store.update(C.KIND_RAYCLUSTER, cluster)
    old_uid = next(pod["metadata"]["uid"] for pod in victims if pod["metadata"]["name"] == victim_name)
    settle(operator, clock)
    replacement = [pod for pod in worker_pods(operator) if pod["metadata"]["name"] == victim_name]
    new_uid = replacement[0]["metadata"]["uid"] if replacement else ""
    cleared = not (operator.store.get(C.KIND_RAYCLUSTER, "lifecycle", "default")["spec"]["workerGroupSpecs"][0].get("scaleStrategy") or {}).get("workersToDelete")
    print(f"victim delete pod={victim_name} replaced={bool(new_uid and new_uid != old_uid)} workersToDelete_cleared={cleared}")
    if not replacement or new_uid == old_uid or not cleared:
        print("  expected the named worker to be replaced and the list cleared")
        failures += 1

    # ------------------------------------------------------------- scale out
    cluster = operator.store.get(C.KIND_RAYCLUSTER, "lifecycle", "default")
    cluster["spec"]["workerGroupSpecs"][0]["replicas"] = 3
    operator.store.update(C.KIND_RAYCLUSTER, cluster)
    settle(operator, clock)
    print(f"scale out     workers={len(worker_pods(operator))}")
    if len(worker_pods(operator)) != 3:
        print("  expected 3 workers")
        failures += 1

    # -------------------------------------------------------------- scale in
    cluster = operator.store.get(C.KIND_RAYCLUSTER, "lifecycle", "default")
    cluster["spec"]["workerGroupSpecs"][0]["replicas"] = 1
    operator.store.update(C.KIND_RAYCLUSTER, cluster)
    settle(operator, clock)
    print(f"scale in      workers={len(worker_pods(operator))}")
    if len(worker_pods(operator)) != 1:
        print("  expected 1 worker")
        failures += 1

    if failures:
        print(f"\nFAILED: {failures} check(s)")
        return 1
    print("\nOK: suspend/resume, Recreate upgrade, victim deletion and scaling converged")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
