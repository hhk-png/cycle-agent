#!/usr/bin/env python3
"""Drive the autoscaler without a Kubernetes cluster.

    python examples/autoscaling_smoke.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from minikuberay import constants as C
from minikuberay.clock import ManualClock
from minikuberay.controllers.base import Operator, OperatorConfig
from minikuberay.models.meta import new_object


def main() -> int:
    clock = ManualClock()
    operator = Operator(clock=clock, config=OperatorConfig())
    cluster = new_object(
        C.RAY_API_VERSION,
        C.KIND_RAYCLUSTER,
        "autoscaling-demo",
        "default",
        spec={
            "rayVersion": "2.59.0",
            "enableInTreeAutoscaling": True,
            "autoscalerOptions": {"idleTimeoutSeconds": 10},
            "headGroupSpec": {
                "template": {
                    "spec": {
                        "containers": [
                            {"name": "ray", "image": "rayproject/ray:2.59.0", "resources": {"requests": {"cpu": "1", "memory": "1Gi"}}}
                        ]
                    }
                }
            },
            "workerGroupSpecs": [
                {
                    "groupName": "workers",
                    "replicas": 0,
                    "minReplicas": 0,
                    "maxReplicas": 4,
                    "template": {
                        "spec": {
                            "containers": [
                                {"name": "ray", "image": "rayproject/ray:2.59.0", "resources": {"requests": {"cpu": "2", "memory": "2Gi"}}}
                            ]
                        }
                    },
                }
            ],
        },
    )
    operator.store.create(C.KIND_RAYCLUSTER, cluster)
    for _ in range(10):
        operator.run_once()
        clock.advance(0.5)
    operator.runtime.add_demand("default", "autoscaling-demo", cpu=6, count=1, ttl_seconds=120)
    for _ in range(12):
        operator.run_once()
        clock.advance(0.5)
    print("after demand:", operator.store.get(C.KIND_RAYCLUSTER, "autoscaling-demo", "default")["spec"]["workerGroupSpecs"][0]["replicas"])
    operator.runtime.clear_demand("default", "autoscaling-demo")
    for _ in range(40):
        operator.run_once()
        clock.advance(1)
    print("after idle  :", operator.store.get(C.KIND_RAYCLUSTER, "autoscaling-demo", "default")["spec"]["workerGroupSpecs"][0]["replicas"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
