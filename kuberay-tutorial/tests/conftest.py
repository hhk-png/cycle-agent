from __future__ import annotations

import pytest

from minikuberay import constants as C
from minikuberay.clock import ManualClock
from minikuberay.controllers.base import Operator, OperatorConfig
from minikuberay.models.meta import new_object


def cluster_spec(
    *,
    workers: int = 2,
    min_replicas: int | None = None,
    max_replicas: int = 6,
    autoscaling: bool = False,
    cpu: str = "1",
    memory: str = "1Gi",
    worker_cpu: str = "2",
    worker_memory: str = "2Gi",
    num_hosts: int = 1,
    group_name: str = "workers",
) -> dict:
    min_replicas = workers if min_replicas is None else min_replicas
    return {
        "rayVersion": "2.59.0",
        "enableInTreeAutoscaling": autoscaling,
        **(
            {"autoscalerOptions": {"idleTimeoutSeconds": 15, "upscalingMode": "Default"}}
            if autoscaling
            else {}
        ),
        "headGroupSpec": {
            "rayStartParams": {},
            "template": {
                "spec": {
                    "containers": [
                        {
                            "name": "ray",
                            "image": "rayproject/ray:2.59.0",
                            "resources": {"requests": {"cpu": cpu, "memory": memory}},
                        }
                    ]
                }
            },
        },
        "workerGroupSpecs": [
            {
                "groupName": group_name,
                "replicas": workers,
                "minReplicas": min_replicas,
                "maxReplicas": max_replicas,
                "numOfHosts": num_hosts,
                "rayStartParams": {},
                "template": {
                    "spec": {
                        "containers": [
                            {
                                "name": "ray",
                                "image": "rayproject/ray:2.59.0",
                                "resources": {"requests": {"cpu": worker_cpu, "memory": worker_memory}},
                            }
                        ]
                    }
                },
            }
        ],
    }


class Harness:
    def __init__(self, *, feature_gates: dict | None = None, config: OperatorConfig | None = None):
        self.clock = ManualClock()
        self.config = config or OperatorConfig()
        if feature_gates:
            self.config.feature_gates.update(feature_gates)
        self.operator = Operator(clock=self.clock, config=self.config)
        self.store = self.operator.store
        self.runtime = self.operator.runtime
        self.endpoints = self.operator.endpoints

    def apply(self, kind: str, name: str, spec: dict, *, namespace: str = "default", labels: dict | None = None, annotations: dict | None = None):
        obj = new_object(
            C.RAY_API_VERSION,
            kind,
            name,
            namespace,
            spec=spec,
            labels=labels,
            annotations=annotations,
        )
        return self.store.create(kind, obj, namespace)

    def create(self, obj: dict):
        return self.store.create(obj["kind"], obj, obj.get("metadata", {}).get("namespace", "default"))

    def get(self, kind: str, name: str, namespace: str = "default") -> dict:
        return self.store.get(kind, name, namespace)

    def list(self, kind: str, namespace: str | None = "default", **kwargs) -> list[dict]:
        return self.store.list(kind, namespace, **kwargs)

    def settle(self, rounds: int = 60, advance: float = 0.0) -> int:
        return self.operator.settle(max_rounds=rounds, advance=advance)

    def step(self, rounds: int = 1, advance: float = 0.0) -> None:
        for _ in range(rounds):
            self.operator.run_once()
            if advance:
                self.clock.advance(advance)


@pytest.fixture
def harness() -> Harness:
    return Harness()


@pytest.fixture
def clock(harness: Harness) -> ManualClock:
    return harness.clock


@pytest.fixture
def spec_factory():
    return cluster_spec
