#!/usr/bin/env python3
"""IPPR lab: watch demand move from vertical to horizontal capacity.

    python examples/ippr_lab.py

The lab runs four deterministic scenarios against MiniKubeRay's reconciler:

1. a valid ``ray.io/ippr`` annotation resizes one running Pod in place;
2. demand beyond ``max-cpu`` spills over into new replicas;
3. a failed resize rolls back and the demand is served horizontally;
4. an invalid annotation is rejected before the autoscaler acts.

Every number is produced by the same controllers the tutorial walks through,
so the expected output in chapter 57 is executable documentation.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from minikuberay import constants as C
from minikuberay.clock import ManualClock
from minikuberay.controllers.base import Operator, OperatorConfig
from minikuberay.models.ippr import parse_ippr, pod_resources, status_of
from minikuberay.models.meta import new_object


def ippr_spec(result: str | None = None) -> dict:
    template_annotations = {C.ANNOTATION_SIM_IPPR_RESULT: result} if result else {}
    return {
        "rayVersion": "2.59.0",
        "enableInTreeAutoscaling": True,
        "autoscalerOptions": {"version": "v2", "upscalingMode": "Default"},
        "headGroupSpec": {
            "rayStartParams": {"num-cpus": "0"},
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
                "groupName": "small",
                "replicas": 1,
                "minReplicas": 1,
                "maxReplicas": 10,
                "rayStartParams": {},
                "template": {
                    "metadata": {"annotations": template_annotations},
                    "spec": {
                        "restartPolicy": "Never",
                        "containers": [
                            {
                                "name": "ray",
                                "image": "rayproject/ray:2.59.0",
                                "resources": {
                                    "requests": {"cpu": "1", "memory": "1Gi"},
                                    "limits": {"cpu": "4", "memory": "4Gi"},
                                },
                                "resizePolicy": [
                                    {"resourceName": "cpu", "restartPolicy": "NotRequired"},
                                    {"resourceName": "memory", "restartPolicy": "NotRequired"},
                                ],
                            }
                        ],
                    },
                },
            }
        ],
    }


def annotation(groups: dict | None = None) -> dict:
    return {
        C.ANNOTATION_IPPR: json.dumps(
            {
                "groups": groups
                if groups is not None
                else {"small": {"max-cpu": "4", "max-memory": "4Gi", "resize-timeout": 60}}
            }
        )
    }


def cluster_object(name: str, *, result: str | None = None, groups: dict | None = None) -> dict:
    obj = new_object(C.RAY_API_VERSION, C.KIND_RAYCLUSTER, name, "default", spec=ippr_spec(result))
    obj["metadata"]["annotations"].update(annotation(groups))
    return obj


def settle(operator: Operator, clock: ManualClock, rounds: int = 40, advance: float = 0.5) -> None:
    for _ in range(rounds):
        operator.run_once()
        clock.advance(advance)


def worker_pods(operator: Operator, name: str) -> list[dict]:
    return sorted(
        (
            pod
            for pod in operator.store.list(C.KIND_POD, "default", label_selector={C.LABEL_CLUSTER: name})
            if (pod["metadata"]["labels"] or {}).get(C.LABEL_NODE_TYPE) == C.NODE_TYPE_WORKER
        ),
        key=lambda pod: pod["metadata"]["name"],
    )


def replicas(operator: Operator, name: str) -> int:
    return int(operator.store.get(C.KIND_RAYCLUSTER, name, "default")["spec"]["workerGroupSpecs"][0]["replicas"])


def run_horizontal_then_vertical() -> int:
    clock = ManualClock()
    operator = Operator(clock=clock, config=OperatorConfig())
    operator.store.create(C.KIND_RAYCLUSTER, cluster_object("ippr-vertical"))
    settle(operator, clock)

    before = worker_pods(operator, "ippr-vertical")
    uid = before[0]["metadata"]["uid"]
    capacities, errors = parse_ippr(operator.store.get(C.KIND_RAYCLUSTER, "ippr-vertical", "default"))
    assert not errors and capacities["small"].max_cpu == 4000
    print(f"1 config      max-cpu=4 max-memory=4Gi timeout=60s groups={sorted(capacities)}")

    # Demand 3 CPU fits entirely into the existing Pod (1 -> 3).
    operator.runtime.add_demand("default", "ippr-vertical", cpu=3, count=1, ttl_seconds=300)
    settle(operator, clock)
    pods = worker_pods(operator, "ippr-vertical")
    print(
        f"2 resize-up   pods={len(pods)} replicas={replicas(operator, 'ippr-vertical')} "
        f"cpu={pod_resources(pods[0]).cpu}m uid_same={pods[0]['metadata']['uid'] == uid} "
        f"state={status_of(pods[0]).get('state')}"
    )
    assert len(pods) == 1 and replicas(operator, "ippr-vertical") == 1
    assert pod_resources(pods[0]).cpu == 3000 and status_of(pods[0])["state"] == "succeeded"
    assert pods[0]["metadata"]["uid"] == uid

    # Another 3 CPU no longer fits in the same Pod: horizontal takes over.
    operator.runtime.add_demand("default", "ippr-vertical", cpu=3, count=1, ttl_seconds=300)
    settle(operator, clock, rounds=60)
    pods = worker_pods(operator, "ippr-vertical")
    cpu = sorted(pod_resources(pod).cpu for pod in pods)
    print(
        f"3 spill-over  pods={len(pods)} replicas={replicas(operator, 'ippr-vertical')} "
        f"cpu={cpu} total={sum(cpu)}m"
    )
    assert len(pods) == 3 and replicas(operator, "ippr-vertical") == 3
    assert cpu == [1000, 1000, 4000]
    return 0


def run_timeout_rollback() -> int:
    clock = ManualClock()
    operator = Operator(clock=clock, config=OperatorConfig())
    operator.store.create(C.KIND_RAYCLUSTER, cluster_object("ippr-timeout", result="timeout"))
    settle(operator, clock)
    operator.runtime.add_demand("default", "ippr-timeout", cpu=2, count=1, ttl_seconds=300)
    settle(operator, clock, rounds=80)

    pods = worker_pods(operator, "ippr-timeout")
    state = status_of(pods[0]).get("state")
    resource = pod_resources(pods[0]).cpu
    print(
        f"4 rollback    pods={len(pods)} replicas={replicas(operator, 'ippr-timeout')} "
        f"cpu={resource}m state={state}"
    )
    assert len(pods) == 2 and replicas(operator, "ippr-timeout") == 2
    assert resource == 1000 and state == "rolled-back"

    cluster = operator.store.get(C.KIND_RAYCLUSTER, "ippr-timeout", "default")
    reasons = [event.get("reason") for event in operator.recorder.list_for(cluster)]
    resize_actions = sorted(str(reason) for reason in reasons if "Resize" in str(reason))
    print(f"5 events      resize_actions={resize_actions}")
    assert "AutoscalerResizeRolledBack" in reasons
    return 0


def run_invalid_annotation() -> int:
    clock = ManualClock()
    operator = Operator(clock=clock, config=OperatorConfig())
    groups = {"small": {"max-cpu": "500m", "max-memory": "1Gi", "resize-timeout": 60}}
    operator.store.create(C.KIND_RAYCLUSTER, cluster_object("ippr-invalid", groups=groups))
    settle(operator, clock, rounds=10)
    cluster = operator.store.get(C.KIND_RAYCLUSTER, "ippr-invalid", "default")
    status = cluster.get("status") or {}
    reasons = [event.get("reason") for event in operator.recorder.list_for(cluster)]
    print(
        f"6 validation  reason={status.get('reason')} "
        f"message={str(status.get('message'))[:48]!r} events={reasons}"
    )
    assert status.get("reason") == "ValidationFailed"
    assert "InvalidRayClusterSpec" in reasons
    assert not worker_pods(operator, "ippr-invalid")
    return 0


def main() -> int:
    failures = 0
    for label, check in (
        ("vertical + horizontal", run_horizontal_then_vertical),
        ("timeout and rollback", run_timeout_rollback),
        ("annotation validation", run_invalid_annotation),
    ):
        try:
            check()
        except AssertionError as exc:
            failures += 1
            print(f"FAILED: {label}: {exc}")
    if failures:
        print(f"{failures} scenario(s) failed")
        return 1
    print(
        "OK: IPPR resized a running Pod in place, fell back to horizontal capacity, "
        "rolled back a timed-out resize, and rejected an invalid annotation."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
