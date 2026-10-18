#!/usr/bin/env python3
"""Run the same RayJob through all four submission modes.

    python examples/rayjob_modes.py

The point of the exercise is *not* that a fake job succeeds.  It is to make the
state-machine differences visible:

* K8sJobMode  -- the Ray job is only submitted after the submitter Pod runs;
* HTTPMode    -- the controller submits as soon as the RayCluster is ready;
* SidecarMode -- the submitter lives inside the head Pod, so no extra Job;
* InteractiveMode -- the controller waits for a user-supplied ``spec.jobId``.

Each mode gets its own RayCluster and its own RayJob.  A ManualClock keeps the
run deterministic, so its output can be asserted in CI.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from minikuberay import constants as C
from minikuberay.clock import ManualClock
from minikuberay.controllers.base import Operator, OperatorConfig
from minikuberay.models.meta import new_object


MODES = ("K8sJobMode", "HTTPMode", "SidecarMode", "InteractiveMode")


def cluster_spec() -> dict:
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
                "replicas": 1,
                "minReplicas": 1,
                "maxReplicas": 2,
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


def rayjob(mode: str) -> dict:
    spec = {
        "entrypoint": "python -c \"print('hello from ' + '" + mode + "')\"",
        "submissionMode": mode,
        "shutdownAfterJobFinishes": False,
        "rayClusterSpec": cluster_spec(),
    }
    if mode == "InteractiveMode":
        # The user owns submission; the operator must not invent an ID.
        spec["jobId"] = ""
    return new_object(C.RAY_API_VERSION, C.KIND_RAYJOB, f"modes-{mode.lower()}", "default", spec=spec)


def submit_interactively(operator: Operator, rayjob_name: str) -> str:
    """Play the role of a Notebook user for InteractiveMode.

    The controller moves the RayJob to ``Waiting`` once the RayCluster is
    ready.  At that point the user submits through the Dashboard API and writes
    the returned job ID into ``spec.jobId``.  We reproduce both steps against
    the simulated runtime.
    """
    job = operator.store.get(C.KIND_RAYJOB, rayjob_name, "default")
    cluster_name = (job.get("status") or {}).get("rayClusterName")
    if not cluster_name:
        return ""
    job_id = f"{rayjob_name}-user-job"
    operator.runtime.submit_job(
        "default",
        cluster_name,
        job_id,
        (job.get("spec") or {}).get("entrypoint", ""),
        duration_seconds=1.0,
        submission_id=job_id,
    )
    job = operator.store.get(C.KIND_RAYJOB, rayjob_name, "default")
    job.setdefault("spec", {})["jobId"] = job_id
    operator.store.update(C.KIND_RAYJOB, job)
    return job_id


def main() -> int:
    clock = ManualClock()
    config = OperatorConfig()
    operator = Operator(clock=clock, config=config)

    for mode in MODES:
        operator.store.create(C.KIND_RAYJOB, rayjob(mode), "default")

    interactive_submitted = False
    history: dict[str, list[str]] = {mode: [] for mode in MODES}

    for _ in range(120):
        operator.run_once()
        for mode in MODES:
            job = operator.store.get(C.KIND_RAYJOB, f"modes-{mode.lower()}", "default")
            status = job.get("status") or {}
            deployment = status.get("jobDeploymentStatus", C.DEPLOYMENT_NEW)
            record = status.get("jobId", "") + "/" + status.get("jobStatus", "")
            if not history[mode] or history[mode][-1] != f"{deployment} {record}".strip():
                history[mode].append(f"{deployment} {record}".strip())
            if mode == "InteractiveMode" and deployment == C.DEPLOYMENT_WAITING and not interactive_submitted:
                submit_interactively(operator, f"modes-{mode.lower()}")
                interactive_submitted = True
        clock.advance(1.0)
        if all(
            (operator.store.get(C.KIND_RAYJOB, f"modes-{mode.lower()}", "default").get("status") or {}).get("jobDeploymentStatus")
            in (C.DEPLOYMENT_COMPLETE, C.DEPLOYMENT_FAILED)
            for mode in MODES
        ):
            break

    print("== state transitions ==")
    for mode in MODES:
        print(f"\n{mode}")
        for line in history[mode]:
            print(f"  {line}")

    print("\n== final status ==")
    failures = 0
    for mode in MODES:
        job = operator.store.get(C.KIND_RAYJOB, f"modes-{mode.lower()}", "default")
        status = job.get("status") or {}
        deployment = status.get("jobDeploymentStatus")
        job_status = status.get("jobStatus")
        cluster_name = status.get("rayClusterName", "-")
        print(f"{mode:<16} deployment={deployment:<9} job={job_status:<9} cluster={cluster_name}")
        if deployment != C.DEPLOYMENT_COMPLETE or job_status != C.JOB_STATUS_SUCCEEDED:
            failures += 1

    # Each mode should have produced exactly one RayCluster and one Ray job.
    clusters = [item for item in operator.store.list(C.KIND_RAYCLUSTER, "default") if item["metadata"]["name"].startswith("modes-")]
    submitter_jobs = operator.store.list(C.KIND_JOB, "default")
    print(f"\nRayClusters={len(clusters)} submitterJobs={len(submitter_jobs)}")
    if len(clusters) != len(MODES):
        print("unexpected RayCluster count")
        failures += 1
    if len(submitter_jobs) != 1:
        # Only K8sJobMode creates a Kubernetes submitter Job.
        print("expected exactly one Kubernetes submitter Job (K8sJobMode)")
        failures += 1

    if failures:
        print(f"\nFAILED: {failures} check(s)")
        return 1
    print("\nOK: all four submission modes reached Complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
