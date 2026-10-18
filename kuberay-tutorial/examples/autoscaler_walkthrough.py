#!/usr/bin/env python3
"""Walk through MiniKubeRay's autoscaler decisions.

Run from the tutorial root:

    python examples/autoscaler_walkthrough.py

The script is deliberately deterministic: it uses ManualClock and a small
RayRuntime, so the output is identical on every machine.  It demonstrates the
three decisions that matter in production:

1. multi-resource demand (CPU + GPU) is rounded up per resource;
2. Conservative mode limits how many replicas can be added per round;
3. scale-down names victims through workersToDelete instead of deleting Pods
   at random.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from minikuberay import constants as C
from minikuberay.autoscaler.policy import AutoscalerPolicy
from minikuberay.clock import ManualClock
from minikuberay.models.defaults import apply_defaults
from minikuberay.models.meta import new_object
from minikuberay.runtime.cluster_runtime import RayRuntime


def make_cluster(
    name: str,
    *,
    replicas: int = 0,
    min_replicas: int = 0,
    max_replicas: int = 10,
    cpu: str = "8",
    gpu: str = "1",
    memory: str = "64Gi",
    num_hosts: int = 1,
    mode: str = "Default",
    idle_timeout: int = 60,
    priority: int = 0,
) -> dict:
    spec = {
        "rayVersion": C.DEFAULT_RAY_VERSION,
        "enableInTreeAutoscaling": True,
        "autoscalerOptions": {
            "version": "v2",
            "upscalingMode": mode,
            "idleTimeoutSeconds": idle_timeout,
        },
        "headGroupSpec": {
            "rayStartParams": {},
            "template": {
                "spec": {
                    "containers": [
                        {
                            "name": "ray",
                            "image": C.DEFAULT_IMAGE,
                            "resources": {"requests": {"cpu": "1", "memory": "2Gi"}},
                        }
                    ]
                }
            },
        },
        "workerGroupSpecs": [
            {
                "groupName": "workers",
                "replicas": replicas,
                "minReplicas": min_replicas,
                "maxReplicas": max_replicas,
                "numOfHosts": num_hosts,
                "priority": priority,
                "rayStartParams": {},
                "template": {
                    "spec": {
                        "containers": [
                            {
                                "name": "ray",
                                "image": C.DEFAULT_IMAGE,
                                "resources": {
                                    "requests": {
                                        "cpu": cpu,
                                        "memory": memory,
                                        "nvidia.com/gpu": gpu,
                                    }
                                },
                            }
                        ]
                    }
                },
            }
        ],
    }
    obj = new_object(C.RAY_API_VERSION, C.KIND_RAYCLUSTER, name, "default", spec=spec)
    return apply_defaults(obj)


def register_worker(runtime: RayRuntime, cluster: str, index: int) -> None:
    pod = {
        "metadata": {
            "name": f"{cluster}-workers-{index}",
            "namespace": "default",
            "labels": {
                C.LABEL_CLUSTER: cluster,
                C.LABEL_NODE_TYPE: C.NODE_TYPE_WORKER,
                C.LABEL_GROUP: "workers",
            },
        },
        "spec": {
            "containers": [
                {
                    "name": "ray",
                    "resources": {
                        "requests": {
                            "cpu": "8",
                            "memory": "64Gi",
                            "nvidia.com/gpu": "1",
                        }
                    },
                }
            ]
        },
    }
    runtime.register_pod(pod)


def show(title: str, cluster: dict, decisions) -> None:
    group = (cluster.get("spec") or {}).get("workerGroupSpecs", [{}])[0]
    print(f"\n== {title} ==")
    print(
        "replicas={replicas} min={min} max={max} hosts={hosts} mode={mode}".format(
            replicas=group.get("replicas"),
            min=group.get("minReplicas"),
            max=group.get("maxReplicas"),
            hosts=group.get("numOfHosts", 1),
            mode=(cluster.get("spec", {}).get("autoscalerOptions") or {}).get("upscalingMode"),
        )
    )
    if not decisions:
        print("  no decision (autoscaling disabled/suspended)")
        return
    for decision in decisions:
        print(
            f"  {decision.group}: {decision.action} {decision.current} -> {decision.desired} "
            f"({decision.reason})"
        )
        if decision.workers_to_delete:
            print(f"    workersToDelete={decision.workers_to_delete}")


def main() -> None:
    # Scenario 1: multi-resource demand.
    clock = ManualClock()
    runtime = RayRuntime(clock)
    cluster = make_cluster("scale-up", max_replicas=10)
    runtime.add_demand("default", "scale-up", cpu=120, gpu=7)
    show("multi-resource demand: 120 CPU + 7 GPU", cluster, AutoscalerPolicy(runtime).decide(cluster))

    # Scenario 2: Conservative mode from 1 replica.
    clock = ManualClock()
    runtime = RayRuntime(clock)
    cluster = make_cluster("conservative", replicas=1, max_replicas=10, mode="Conservative")
    runtime.add_demand("default", "conservative", cpu=120, gpu=7)
    show("Conservative mode: 1 replica, same demand", cluster, AutoscalerPolicy(runtime).decide(cluster))

    # Scenario 3: multi-host capacity (one replica = two Pods).
    clock = ManualClock()
    runtime = RayRuntime(clock)
    cluster = make_cluster("multi-host", replicas=1, max_replicas=10, num_hosts=2)
    runtime.add_demand("default", "multi-host", cpu=120, gpu=7)
    show("multi-host: numOfHosts=2", cluster, AutoscalerPolicy(runtime).decide(cluster))

    # Scenario 4: idle scale-down with named victims.
    clock = ManualClock()
    runtime = RayRuntime(clock)
    cluster = make_cluster("scale-down", replicas=3, max_replicas=10, idle_timeout=60)
    for index in range(3):
        register_worker(runtime, "scale-down", index)
    clock.advance(120)  # all three workers have been idle for 120s
    show("idle scale-down after 120s", cluster, AutoscalerPolicy(runtime).decide(cluster))

    print("\nAll decisions were produced by minikuberay/autoscaler/policy.py.")


if __name__ == "__main__":
    main()
