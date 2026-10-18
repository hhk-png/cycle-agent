#!/usr/bin/env python3
"""Runnable incident laboratory for MiniKubeRay.

Each scenario reproduces a production failure mode from chapter 37 with the
deterministic clock and the simulated control plane.  The script asserts the
same invariants the tutorials describe, so it doubles as an executable
regression suite for the *control-plane behaviour* (not for real Ray).

Usage:

    python examples/incident_lab.py                 # run every scenario
    python examples/incident_lab.py --scenario all
    python examples/incident_lab.py --scenario worker-crash
    python examples/incident_lab.py --list

The lab intentionally uses only the public ``minikuberay`` package, so it can
be read and modified without understanding the test harness.
"""

from __future__ import annotations

import argparse
import sys
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from minikuberay import constants as C
from minikuberay.clock import ManualClock
from minikuberay.controllers.base import Operator, OperatorConfig
from minikuberay.models.core import pod_is_ready, pod_phase
from minikuberay.models.meta import new_object, uid


# ---------------------------------------------------------------------------
# Test doubles and helpers
# ---------------------------------------------------------------------------
def cluster_spec(
    *,
    workers: int = 2,
    min_replicas: int | None = None,
    max_replicas: int = 6,
    autoscaling: bool = False,
    num_hosts: int = 1,
    image: str = C.DEFAULT_IMAGE,
    head_annotations: dict | None = None,
    worker_annotations: dict | None = None,
) -> dict:
    min_replicas = workers if min_replicas is None else min_replicas
    head_meta: dict = {}
    worker_meta: dict = {}
    if head_annotations:
        head_meta["annotations"] = dict(head_annotations)
    if worker_annotations:
        worker_meta["annotations"] = dict(worker_annotations)
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
                **({"metadata": head_meta} if head_meta else {}),
                "spec": {
                    "containers": [
                        {
                            "name": "ray",
                            "image": image,
                            "resources": {"requests": {"cpu": "1", "memory": "1Gi"}},
                        }
                    ]
                },
            },
        },
        "workerGroupSpecs": [
            {
                "groupName": "workers",
                "replicas": workers,
                "minReplicas": min_replicas,
                "maxReplicas": max_replicas,
                "numOfHosts": num_hosts,
                "rayStartParams": {},
                "template": {
                    **({"metadata": worker_meta} if worker_meta else {}),
                    "spec": {
                        "containers": [
                            {
                                "name": "ray",
                                "image": image,
                                "resources": {"requests": {"cpu": "2", "memory": "2Gi"}},
                            }
                        ]
                    },
                },
            }
        ],
    }


class Lab:
    """A tiny operator sandbox with an explicit clock."""

    def __init__(self, *, feature_gates: dict | None = None) -> None:
        self.clock = ManualClock()
        self.config = OperatorConfig()
        if feature_gates:
            self.config.feature_gates.update(feature_gates)
        self.operator = Operator(clock=self.clock, config=self.config)
        self.store = self.operator.store
        self.runtime = self.operator.runtime

    def apply(
        self,
        kind: str,
        name: str,
        spec: dict,
        *,
        namespace: str = "default",
        labels: dict | None = None,
        annotations: dict | None = None,
    ) -> dict:
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

    def get(self, kind: str, name: str, namespace: str = "default") -> dict:
        return self.store.get(kind, name, namespace)

    def pods(self, cluster: str, namespace: str = "default") -> list[dict]:
        return self.store.list(C.KIND_POD, namespace, label_selector={C.LABEL_CLUSTER: cluster})

    def step(self, rounds: int = 1, advance: float = 0.0) -> None:
        for _ in range(rounds):
            self.operator.run_once()
            if advance:
                self.clock.advance(advance)

    def settle(self, rounds: int = 80, advance: float = 0.0) -> int:
        return self.operator.settle(max_rounds=rounds, advance=advance)


@dataclass
class Result:
    name: str
    passed: bool
    observed: str
    detail: list[str] = field(default_factory=list)


def _event_reasons(lab: Lab, kind: str, name: str) -> list[str]:
    namespace = "default"
    return [
        (event.get("reason") or "")
        for event in lab.store.list(C.KIND_EVENT, namespace)
        if (event.get("involvedObject") or {}).get("name") == name
        or (event.get("regarding") or {}).get("name") == name
        or (event.get("metadata") or {}).get("name", "").startswith(name)
    ]


def _worker_uids(lab: Lab, cluster: str) -> dict[str, str]:
    return {
        pod["metadata"]["name"]: uid(pod)
        for pod in lab.pods(cluster)
        if (pod["metadata"].get("labels") or {}).get(C.LABEL_NODE_TYPE) == C.NODE_TYPE_WORKER
    }


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------
def scenario_worker_crash() -> Result:
    """A worker Pod crashes; the controller must replace it, not shrink."""

    lab = Lab()
    spec = cluster_spec(
        workers=2,
        worker_annotations={C.ANNOTATION_SIM_CRASH_AFTER: "5"},
    )
    lab.apply(C.KIND_RAYCLUSTER, "crash-workers", spec)
    lab.settle(40)
    before = _worker_uids(lab, "crash-workers")
    lab.step(10, advance=1.0)  # cross the crash deadline
    lab.settle(40)
    after = _worker_uids(lab, "crash-workers")
    replaced = sum(1 for name, value in after.items() if before.get(name) not in (None, value))
    ready = sum(
        1
        for pod in lab.pods("crash-workers")
        if (pod["metadata"].get("labels") or {}).get(C.LABEL_NODE_TYPE) == C.NODE_TYPE_WORKER
        and pod_is_ready(pod)
        and pod_phase(pod) == "Running"
    )
    status = lab.get(C.KIND_RAYCLUSTER, "crash-workers")["status"]
    passed = replaced >= 1 and len(after) == 2 and status.get("state") == "ready"
    return Result(
        "worker-crash",
        passed,
        f"workers={len(after)} replaced={replaced} ready={ready} state={status.get('state')}",
        [f"before={sorted(before)}", f"after={sorted(after)}"],
    )


def scenario_head_crash() -> Result:
    """Without GCS FT a head crash is a cluster rebuild; Pods must converge again."""

    lab = Lab()
    spec = cluster_spec(
        workers=1,
        head_annotations={C.ANNOTATION_SIM_CRASH_AFTER: "5"},
    )
    lab.apply(C.KIND_RAYCLUSTER, "crash-head", spec)
    lab.settle(40)
    head_before = next(
        pod for pod in lab.pods("crash-head")
        if (pod["metadata"].get("labels") or {}).get(C.LABEL_NODE_TYPE) == C.NODE_TYPE_HEAD
    )
    old_uid = uid(head_before)
    lab.step(10, advance=1.0)
    lab.settle(60)
    pods = lab.pods("crash-head")
    head_after = next(
        pod for pod in pods
        if (pod["metadata"].get("labels") or {}).get(C.LABEL_NODE_TYPE) == C.NODE_TYPE_HEAD
    )
    status = lab.get(C.KIND_RAYCLUSTER, "crash-head")["status"]
    passed = uid(head_after) != old_uid and status.get("state") == "ready"
    return Result(
        "head-crash",
        passed,
        f"head_replaced={uid(head_after) != old_uid} state={status.get('state')} workers={sum(1 for p in pods if (p['metadata'].get('labels') or {}).get(C.LABEL_NODE_TYPE) == 'worker')}",
        [f"old_uid={old_uid[:8]}", f"new_uid={uid(head_after)[:8]}"],
    )


def scenario_rayjob_retry() -> Result:
    """A failing entrypoint consumes backoffLimit and ends in a terminal state."""

    lab = Lab()
    spec = {
        "entrypoint": "python -c \"raise SystemExit(1)\"",
        "submissionMode": "K8sJobMode",
        "shutdownAfterJobFinishes": True,
        "ttlSecondsAfterFinished": 0,
        "backoffLimit": 1,
        "rayClusterSpec": cluster_spec(workers=0),
    }
    lab.apply(
        C.KIND_RAYJOB,
        "always-fails",
        spec,
        annotations={
            C.ANNOTATION_SIM_JOB_DURATION: "1",
            C.ANNOTATION_SIM_JOB_EXIT_CODE: "1",
        },
    )
    lab.settle(120, advance=1.0)
    status = lab.get(C.KIND_RAYJOB, "always-fails")["status"]
    passed = (
        status.get("jobDeploymentStatus") == C.DEPLOYMENT_FAILED
        and int(status.get("failed", 0) or 0) >= 2
        and status.get("jobStatus") == C.JOB_STATUS_FAILED
    )
    return Result(
        "rayjob-retry",
        passed,
        f"deployment={status.get('jobDeploymentStatus')} job={status.get('jobStatus')} failed={status.get('failed')}",
        [f"reason={status.get('reason')}", f"message={status.get('message')}"],
    )


def scenario_deadline() -> Result:
    """activeDeadlineSeconds is a hard stop, independent of the entrypoint."""

    lab = Lab()
    spec = {
        "entrypoint": "python -c \"import time; time.sleep(3600)\"",
        "submissionMode": "HTTPMode",
        "shutdownAfterJobFinishes": True,
        "activeDeadlineSeconds": 5,
        "rayClusterSpec": cluster_spec(workers=0),
    }
    lab.apply(
        C.KIND_RAYJOB,
        "slow-job",
        spec,
        annotations={C.ANNOTATION_SIM_JOB_DURATION: "3600"},
    )
    lab.settle(120, advance=1.0)
    status = lab.get(C.KIND_RAYJOB, "slow-job")["status"]
    passed = (
        status.get("jobDeploymentStatus") == C.DEPLOYMENT_FAILED
        and status.get("reason") == "DeadlineExceeded"
    )
    return Result(
        "deadline",
        passed,
        f"deployment={status.get('jobDeploymentStatus')} reason={status.get('reason')}",
        [f"message={status.get('message')}"],
    )


def scenario_serve_slow_deploy() -> Result:
    """A slow model upload keeps the RayService not-Ready without flapping."""

    lab = Lab()
    spec = {
        "serveConfigV2": "applications:\n  - name: app\n    import_path: app:deployment\n    route_prefix: /\n",
        "rayClusterConfig": cluster_spec(workers=1),
    }
    lab.apply(
        C.KIND_RAYSERVICE,
        "slow-serve",
        spec,
        annotations={C.ANNOTATION_SIM_DEPLOY_DELAY: "30"},
    )
    # ``settle`` stops as soon as nothing changes, so it must not be used to
    # advance the clock across a deployment delay.  ``step`` advances the
    # ManualClock by a fixed amount on every round.
    lab.step(10, advance=1.0)
    lab.settle(20)
    early = lab.get(C.KIND_RAYSERVICE, "slow-serve")["status"]
    lab.step(30, advance=1.0)
    lab.settle(30)
    late = lab.get(C.KIND_RAYSERVICE, "slow-serve")["status"]
    early_ready = any(
        condition.get("type") == C.COND_READY and condition.get("status") == "True"
        for condition in early.get("conditions") or []
    )
    late_ready = any(
        condition.get("type") == C.COND_READY and condition.get("status") == "True"
        for condition in late.get("conditions") or []
    )
    passed = (not early_ready) and late_ready and late.get("numServeEndpoints", 0) > 0
    return Result(
        "serve-slow-deploy",
        passed,
        f"early_ready={early_ready} late_ready={late_ready} endpoints={late.get('numServeEndpoints')}",
        [f"serviceStatus={late.get('serviceStatus')}"],
    )


def scenario_serve_rollback() -> Result:
    """Reverting the spec during an upgrade must drop the pending cluster."""

    lab = Lab()
    original = cluster_spec(workers=1, image="rayproject/ray:2.59.0")
    spec = {
        "serveConfigV2": "applications:\n  - name: app\n    import_path: app:v1\n    route_prefix: /\n",
        "rayClusterConfig": original,
    }
    # Keep the new cluster in DEPLOYING long enough to observe the pending
    # state; otherwise the simulated Serve runtime is ready immediately and the
    # upgrade promotes in the first reconcile.
    lab.apply(
        C.KIND_RAYSERVICE,
        "rollback",
        spec,
        annotations={C.ANNOTATION_SIM_DEPLOY_DELAY: "20"},
    )
    lab.step(25, advance=1.0)
    lab.settle(30)
    before = lab.get(C.KIND_RAYSERVICE, "rollback")["status"].get("activeServiceStatus", {}).get("rayClusterName")
    changed = lab.get(C.KIND_RAYSERVICE, "rollback")
    changed["spec"]["rayClusterConfig"]["headGroupSpec"]["template"]["spec"]["containers"][0]["image"] = "rayproject/ray:2.59.1"
    lab.store.update(C.KIND_RAYSERVICE, changed)
    lab.step(5, advance=1.0)
    lab.settle(20)
    pending = lab.get(C.KIND_RAYSERVICE, "rollback")["status"].get("pendingServiceStatus", {}).get("rayClusterName")
    reverted = lab.get(C.KIND_RAYSERVICE, "rollback")
    reverted["spec"]["rayClusterConfig"]["headGroupSpec"]["template"]["spec"]["containers"][0]["image"] = "rayproject/ray:2.59.0"
    lab.store.update(C.KIND_RAYSERVICE, reverted)
    lab.step(5, advance=1.0)
    lab.settle(40)
    after = lab.get(C.KIND_RAYSERVICE, "rollback")["status"]
    pending_after = after.get("pendingServiceStatus", {}).get("rayClusterName")
    passed = bool(pending) and pending_after in (None, "") and before == after.get("activeServiceStatus", {}).get("rayClusterName")
    return Result(
        "serve-rollback",
        passed,
        f"pending_seen={bool(pending)} pending_after={pending_after or '-'} active_stable={before == after.get('activeServiceStatus', {}).get('rayClusterName')}",
        [f"active={before}"],
    )


def scenario_gcs_ft_cleanup_timeout() -> Result:
    """A stuck Redis cleanup Job is released by the deletion timeout."""

    lab = Lab()
    spec = cluster_spec(workers=1)
    spec["gcsFaultToleranceOptions"] = {
        "backend": "redis",
        "redisAddress": "redis.default.svc.cluster.local:6379",
        "externalStorageNamespace": "ft-lab",
    }
    lab.apply(
        C.KIND_RAYCLUSTER,
        "ft-cleanup",
        spec,
        annotations={C.ANNOTATION_GCS_FT_DELETION_TIMEOUT: "5s"},
    )
    lab.settle(40)
    lab.store.delete(C.KIND_RAYCLUSTER, "ft-cleanup", "default")
    lab.settle(10)
    # Make the cleanup Job's Pod hang beyond the finalizer timeout.
    try:
        pod = lab.get(C.KIND_POD, "ft-cleanup-redis-cleanup-submitter")
        pod.setdefault("metadata", {}).setdefault("annotations", {})[C.ANNOTATION_SIM_JOB_COMPLETE] = "9999"
        lab.store.update(C.KIND_POD, pod)
    except Exception:
        pass
    lab.step(12, advance=1.0)
    lab.settle(30)
    timed_out = "GCSFTRedisCleanupTimeout" in _event_reasons(lab, C.KIND_RAYCLUSTER, "ft-cleanup")
    try:
        cluster = lab.get(C.KIND_RAYCLUSTER, "ft-cleanup")
        remaining = cluster.get("metadata", {}).get("finalizers") or []
        passed = timed_out and C.FINALIZER_GCS_FT not in remaining
        observed = f"timeout_event={timed_out} finalizers={remaining}"
    except Exception:
        passed = timed_out
        observed = f"timeout_event={timed_out} RayCluster deleted"
    return Result("gcs-ft-cleanup-timeout", passed, observed)


def scenario_multihost_replacement() -> Result:
    """Deleting one host replaces the whole replica, never half a slice."""

    lab = Lab()
    lab.apply(C.KIND_RAYCLUSTER, "multi", cluster_spec(workers=2, num_hosts=2))
    lab.settle(50)
    pods = lab.pods("multi")
    replica_names = {
        (pod["metadata"].get("labels") or {}).get(C.LABEL_WORKER_REPLICA_NAME)
        for pod in pods
        if (pod["metadata"].get("labels") or {}).get(C.LABEL_NODE_TYPE) == C.NODE_TYPE_WORKER
    }
    replica_names.discard(None)
    victim = next(
        pod for pod in pods
        if (pod["metadata"].get("labels") or {}).get(C.LABEL_NODE_TYPE) == C.NODE_TYPE_WORKER
    )
    victim_replica = (victim["metadata"].get("labels") or {}).get(C.LABEL_WORKER_REPLICA_NAME)
    peers_before = {
        pod["metadata"]["name"]: uid(pod)
        for pod in pods
        if (pod["metadata"].get("labels") or {}).get(C.LABEL_WORKER_REPLICA_NAME) == victim_replica
    }
    lab.store.delete(C.KIND_POD, victim["metadata"]["name"], "default", force=True)
    lab.settle(50)
    after = lab.pods("multi")
    workers = [
        pod for pod in after
        if (pod["metadata"].get("labels") or {}).get(C.LABEL_NODE_TYPE) == C.NODE_TYPE_WORKER
    ]
    peers_after = {
        pod["metadata"].get("labels", {}).get(C.LABEL_WORKER_REPLICA_NAME): {
            item["metadata"]["name"]: uid(item)
            for item in workers
            if (item["metadata"].get("labels") or {}).get(C.LABEL_WORKER_REPLICA_NAME) == pod["metadata"]["labels"][C.LABEL_WORKER_REPLICA_NAME]
        }
        for pod in workers
    }.get(victim_replica, {})
    replaced_all = set(peers_before) == set(peers_after) and all(
        peers_before[name] != peers_after.get(name) for name in peers_before
    )
    passed = len(workers) == 4 and len(replica_names) == 2 and replaced_all
    return Result(
        "multihost-replacement",
        passed,
        f"workers={len(workers)} replicas={len(replica_names)} whole_replica_replaced={replaced_all}",
        [f"victim_replica={victim_replica}"],
    )


def scenario_invalid_recover() -> Result:
    """ValidationFailed is recoverable once the user fixes the spec."""

    lab = Lab()
    spec = cluster_spec(workers=1)
    spec["workerGroupSpecs"][0]["minReplicas"] = 5
    spec["workerGroupSpecs"][0]["maxReplicas"] = 2
    lab.apply(C.KIND_RAYCLUSTER, "invalid", spec)
    lab.settle(20)
    bad = lab.get(C.KIND_RAYCLUSTER, "invalid")["status"]
    bad_reason = bad.get("reason")
    fixed = lab.get(C.KIND_RAYCLUSTER, "invalid")
    fixed["spec"]["workerGroupSpecs"][0]["minReplicas"] = 1
    fixed["spec"]["workerGroupSpecs"][0]["maxReplicas"] = 3
    lab.store.update(C.KIND_RAYCLUSTER, fixed)
    lab.settle(40)
    good = lab.get(C.KIND_RAYCLUSTER, "invalid")["status"]
    passed = bad_reason == "ValidationFailed" and good.get("state") == "ready"
    return Result(
        "invalid-recover",
        passed,
        f"before={bad_reason} after_state={good.get('state')}",
        [f"after_reason={good.get('reason') or '-'}"],
    )


def scenario_selector_does_not_delete() -> Result:
    """A RayJob using clusterSelector must never delete the shared cluster."""

    lab = Lab()
    lab.apply(
        C.KIND_RAYCLUSTER,
        "shared",
        cluster_spec(workers=1),
        labels={C.LABEL_CLUSTER: "shared"},
    )
    lab.settle(40)
    lab.apply(
        C.KIND_RAYJOB,
        "selector-job",
        {
            "entrypoint": "python -c \"print('shared')\"",
            "submissionMode": "HTTPMode",
            "clusterSelector": {C.LABEL_CLUSTER: "shared"},
        },
        annotations={C.ANNOTATION_SIM_JOB_DURATION: "1"},
    )
    lab.settle(80, advance=1.0)
    job = lab.get(C.KIND_RAYJOB, "selector-job")["status"]
    try:
        cluster = lab.get(C.KIND_RAYCLUSTER, "shared")
        still_there = cluster.get("metadata", {}).get("deletionTimestamp") is None
    except Exception:
        still_there = False
    passed = job.get("jobDeploymentStatus") == C.DEPLOYMENT_COMPLETE and still_there
    return Result(
        "selector-no-delete",
        passed,
        f"job={job.get('jobDeploymentStatus')} shared_cluster={still_there}",
        [f"jobStatus={job.get('jobStatus')}"],
    )


def scenario_autoscaler_idle() -> Result:
    """Demand drives scale-up; idle timeout returns workers to minReplicas."""

    lab = Lab()
    lab.apply(C.KIND_RAYCLUSTER, "auto", cluster_spec(workers=0, min_replicas=0, autoscaling=True))
    lab.settle(30)
    lab.runtime.add_demand("default", "auto", cpu=6, count=1, ttl_seconds=120)
    lab.settle(20)
    up = lab.get(C.KIND_RAYCLUSTER, "auto")["spec"]["workerGroupSpecs"][0]["replicas"]
    lab.runtime.clear_demand("default", "auto")
    lab.step(40, advance=1.0)
    lab.settle(30)
    down = lab.get(C.KIND_RAYCLUSTER, "auto")["spec"]["workerGroupSpecs"][0]["replicas"]
    passed = up > 0 and down == 0
    return Result("autoscaler-idle", passed, f"replicas_up={up} replicas_down={down}")


SCENARIOS: dict[str, Callable[[], Result]] = {
    "worker-crash": scenario_worker_crash,
    "head-crash": scenario_head_crash,
    "rayjob-retry": scenario_rayjob_retry,
    "deadline": scenario_deadline,
    "serve-slow-deploy": scenario_serve_slow_deploy,
    "serve-rollback": scenario_serve_rollback,
    "gcs-ft-cleanup-timeout": scenario_gcs_ft_cleanup_timeout,
    "multihost-replacement": scenario_multihost_replacement,
    "invalid-recover": scenario_invalid_recover,
    "selector-no-delete": scenario_selector_does_not_delete,
    "autoscaler-idle": scenario_autoscaler_idle,
}


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--scenario",
        default="all",
        help="scenario name, or 'all' (default). Use --list to see names.",
    )
    parser.add_argument("--list", action="store_true", help="list available scenarios")
    parser.add_argument("--verbose", action="store_true", help="print tracebacks for failures")
    args = parser.parse_args(argv)

    if args.list:
        for name in SCENARIOS:
            print(name)
        return 0

    names = list(SCENARIOS) if args.scenario == "all" else [args.scenario]
    unknown = [name for name in names if name not in SCENARIOS]
    if unknown:
        print(f"unknown scenario(s): {', '.join(unknown)}", file=sys.stderr)
        return 2

    results: list[Result] = []
    for name in names:
        try:
            result = SCENARIOS[name]()
        except Exception as exc:  # noqa: BLE001 - the lab must report, not crash
            result = Result(name, False, f"raised {type(exc).__name__}: {exc}")
            if args.verbose:
                traceback.print_exc()
        results.append(result)
        marker = "PASS" if result.passed else "FAIL"
        print(f"[{marker}] {result.name:<26} {result.observed}")
        for line in result.detail:
            print(f"       {line}")

    passed = sum(1 for result in results if result.passed)
    print(f"\n{passed}/{len(results)} scenarios passed (deterministic ManualClock)")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
