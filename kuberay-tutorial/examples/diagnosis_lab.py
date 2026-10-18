#!/usr/bin/env python3
"""Diagnosis lab: build an evidence chain instead of guessing.

    python examples/diagnosis_lab.py

A Ray incident usually produces five kinds of evidence, in increasing cost:

1. CR status/conditions  (control plane decided what?)
2. Pod phase/events      (Kubernetes scheduled and started what?)
3. runtime nodes         (did the raylet register?)
4. job/Serve records     (did the application make progress?)
5. State API + logs      (what was the process doing?)

The lab creates a healthy cluster, submits a job, kills a worker, and then
creates an invalid cluster on purpose.  It queries the same datasets through
the Ray-style ``/api/v0`` endpoints so the commands in chapter 56 can be
checked against real output.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from minikuberay import constants as C
from minikuberay.apiserver.server import ApiServer
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
                "minReplicas": workers,
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


def settle(operator: Operator, clock: ManualClock, rounds: int = 40, advance: float = 0.5) -> None:
    for _ in range(rounds):
        operator.run_once()
        clock.advance(advance)


def worker_pods(operator: Operator, cluster: str) -> list[dict]:
    return [
        pod
        for pod in operator.store.list(C.KIND_POD, "default", label_selector={C.LABEL_CLUSTER: cluster})
        if (pod["metadata"].get("labels") or {}).get(C.LABEL_NODE_TYPE) == C.NODE_TYPE_WORKER
    ]


def main() -> int:
    clock = ManualClock()
    operator = Operator(clock=clock, config=OperatorConfig())
    api = ApiServer(operator.store, runtime=operator.runtime, metrics=operator.metrics.registry, port=0)
    url = api.start()
    failures = 0

    try:
        from minikuberay.kube.client import HTTPClient

        client = HTTPClient(url)

        # ---------------------------------------------------------- healthy path
        operator.store.create(
            C.KIND_RAYCLUSTER,
            new_object(C.RAY_API_VERSION, C.KIND_RAYCLUSTER, "diag", "default", spec=cluster_spec(2)),
            "default",
        )
        settle(operator, clock)
        cluster = operator.store.get(C.KIND_RAYCLUSTER, "diag", "default")
        status = cluster.get("status") or {}
        print(f"1 status      state={status.get('state')} desired={status.get('desiredWorkerReplicas')} ready={status.get('readyWorkerReplicas')}")
        if status.get("state") != "ready":
            print("  expected state=ready")
            failures += 1

        nodes = client.state("nodes", cluster="diag")["data"]["result"]
        print(f"2 nodes       total={nodes['total']} states={sorted({item['state'] for item in nodes['result']})}")
        if nodes["total"] != 3:
            print("  expected head + 2 worker nodes")
            failures += 1

        job = new_object(
            C.RAY_API_VERSION,
            C.KIND_RAYJOB,
            "diag-job",
            "default",
            spec={"entrypoint": "python train.py", "rayClusterSpec": cluster_spec(1)},
        )
        operator.store.create(C.KIND_RAYJOB, job, "default")
        settle(operator, clock, rounds=80)
        jobs = client.state("jobs", namespace="default", filter="python train.py")["data"]["result"]
        job_status = jobs["result"][0]["status"] if jobs["result"] else "missing"
        print(f"3 jobs        total={jobs['total']} status={job_status} entrypoint={jobs['result'][0]['entrypoint'] if jobs['result'] else ''}")
        if job_status != C.JOB_STATUS_SUCCEEDED:
            print("  expected the job to reach SUCCEEDED")
            failures += 1

        # ------------------------------------------------------- worker replaced
        victim = worker_pods(operator, "diag")[0]
        old_uid = victim["metadata"]["uid"]
        operator.store.delete(C.KIND_POD, victim["metadata"]["name"], "default", force=True)
        settle(operator, clock)
        replacement = [pod for pod in worker_pods(operator, "diag") if pod["metadata"]["name"] == victim["metadata"]["name"]]
        new_uid = replacement[0]["metadata"]["uid"] if replacement else ""
        print(f"4 restart     pod={victim['metadata']['name']} replaced={bool(new_uid and new_uid != old_uid)} workers={len(worker_pods(operator, 'diag'))}")
        if not replacement or new_uid == old_uid:
            print("  expected the worker to be recreated with a new UID")
            failures += 1

        # --------------------------------------------------------- invalid spec
        bad = new_object(
            C.RAY_API_VERSION,
            C.KIND_RAYCLUSTER,
            "diag-bad",
            "default",
            spec=cluster_spec(4),
        )
        bad["spec"]["workerGroupSpecs"][0]["minReplicas"] = 5
        bad["spec"]["workerGroupSpecs"][0]["maxReplicas"] = 2
        operator.store.create(C.KIND_RAYCLUSTER, bad, "default")
        settle(operator, clock, rounds=10)
        bad_status = operator.store.get(C.KIND_RAYCLUSTER, "diag-bad", "default").get("status") or {}
        conditions = {item["type"]: item["status"] for item in bad_status.get("conditions") or []}
        print(f"5 invalid     reason={bad_status.get('reason')} provisioned={conditions.get('RayClusterProvisioned')} message={bad_status.get('message', '')[:60]}")
        if bad_status.get("reason") != "ValidationFailed" or conditions.get(C.COND_PROVISIONED) != "False":
            print("  expected ValidationFailed and RayClusterProvisioned=False")
            failures += 1

        events = client.state("events", cluster="diag-bad", filter="Invalid")["data"]["result"]
        reasons = sorted({item.get("reason") for item in events["result"]})
        print(f"6 events      total={events['total']} reasons={reasons}")
        if "InvalidRayClusterSpec" not in reasons:
            print("  expected an InvalidRayClusterSpec event")
            failures += 1

        # The evidence chain is ordered: status says what, events say why.
        print("\norder: status/conditions -> Pods -> runtime nodes -> jobs -> events -> logs")
    finally:
        api.stop()

    if failures:
        print(f"\nFAILED: {failures} check(s)")
        return 1
    print("\nOK: status, State API nodes/jobs, self-healing and validation events form one evidence chain")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
