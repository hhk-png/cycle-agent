#!/usr/bin/env python3
"""Control-plane scale benchmark for MiniKubeRay.

This is not a Ray data-plane benchmark.  It measures the part of the operator
that grows with the number of objects: watch/store bookkeeping, reconcile
scans, API writes and garbage collection.  Use it to reason about the numbers
in chapter 41 and to compare changes to the controllers.

    python examples/scale_benchmark.py --clusters 100 --workers 2
    python examples/scale_benchmark.py --clusters 500 --workers 0 --json

The clock is manual, so timings are wall-clock measurements of *this* Python
process; the object counts and resourceVersion deltas are deterministic.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from minikuberay import constants as C
from minikuberay.clock import ManualClock
from minikuberay.controllers.base import Operator, OperatorConfig
from minikuberay.models.meta import new_object


def tiny_cluster_spec(*, workers: int) -> dict:
    """A deliberately small Pod shape so the benchmark measures objects, not CPU."""

    return {
        "rayVersion": "2.59.0",
        "headGroupSpec": {
            "rayStartParams": {},
            "template": {
                "spec": {
                    "containers": [
                        {
                            "name": "ray",
                            "image": C.DEFAULT_IMAGE,
                            "resources": {"requests": {"cpu": "100m", "memory": "128Mi"}},
                        }
                    ]
                }
            },
        },
        "workerGroupSpecs": [
            {
                "groupName": "workers",
                "replicas": workers,
                "minReplicas": 0,
                "maxReplicas": max(workers, 8),
                "rayStartParams": {},
                "template": {
                    "spec": {
                        "containers": [
                            {
                                "name": "ray",
                                "image": C.DEFAULT_IMAGE,
                                "resources": {"requests": {"cpu": "100m", "memory": "128Mi"}},
                            }
                        ]
                    }
                },
            }
        ],
    }


def object_counts(operator: Operator) -> dict[str, int]:
    kinds = [
        C.KIND_RAYCLUSTER,
        C.KIND_POD,
        C.KIND_SERVICE,
        C.KIND_ENDPOINTSLICE,
        C.KIND_SERVICEACCOUNT,
        C.KIND_ROLE,
        C.KIND_ROLEBINDING,
        C.KIND_SECRET,
        C.KIND_EVENT,
    ]
    return {kind: len(operator.store.list(kind, None)) for kind in kinds}


def approximate_bytes(operator: Operator) -> int:
    """A rough JSON footprint of the objects the operator caches."""

    total = 0
    for kind in (
        C.KIND_RAYCLUSTER,
        C.KIND_RAYJOB,
        C.KIND_RAYSERVICE,
        C.KIND_RAYCRONJOB,
        C.KIND_POD,
        C.KIND_SERVICE,
        C.KIND_ENDPOINTSLICE,
        C.KIND_EVENT,
    ):
        total += len(json.dumps(operator.store.list(kind, None), ensure_ascii=False).encode("utf-8"))
    return total


def timed_settle(operator: Operator, rounds: int) -> tuple[int, float]:
    started = time.perf_counter()
    used = operator.settle(max_rounds=rounds)
    return used, time.perf_counter() - started


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--clusters", type=int, default=100)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--rounds", type=int, default=120)
    parser.add_argument("--json", action="store_true", help="emit a machine-readable summary")
    args = parser.parse_args(argv)

    if args.clusters < 1:
        parser.error("--clusters must be >= 1")

    clock = ManualClock()
    config = OperatorConfig()
    operator = Operator(clock=clock, config=config)

    create_started = time.perf_counter()
    for index in range(args.clusters):
        name = f"scale-{index:04d}"
        operator.store.create(
            C.KIND_RAYCLUSTER,
            new_object(
                C.RAY_API_VERSION,
                C.KIND_RAYCLUSTER,
                name,
                "default",
                spec=tiny_cluster_spec(workers=args.workers),
            ),
        )
    create_elapsed = time.perf_counter() - create_started
    rv_after_create = operator.store.resource_version

    rounds_create, settle_create = timed_settle(operator, args.rounds)
    counts = object_counts(operator)
    rv_after_settle = operator.store.resource_version
    footprint = approximate_bytes(operator)

    # Scale every worker group up by one replica, then reconcile.
    rv_before_scale = operator.store.resource_version
    for cluster in operator.store.list(C.KIND_RAYCLUSTER, None):
        cluster["spec"]["workerGroupSpecs"][0]["replicas"] = int(
            cluster["spec"]["workerGroupSpecs"][0].get("replicas", 0)
        ) + 1
        operator.store.update(C.KIND_RAYCLUSTER, cluster)
    rv_after_patch = operator.store.resource_version
    rounds_scale, settle_scale = timed_settle(operator, args.rounds)
    rv_after_scale = operator.store.resource_version

    # Delete everything and let ownerReference/GC converge.
    for cluster in operator.store.list(C.KIND_RAYCLUSTER, None):
        operator.store.delete(C.KIND_RAYCLUSTER, cluster["metadata"]["name"], "default")
    rounds_delete, settle_delete = timed_settle(operator, args.rounds)
    remaining = object_counts(operator)

    summary = {
        "clusters": args.clusters,
        "workers_per_cluster": args.workers,
        "create_seconds": round(create_elapsed, 4),
        "converge_rounds": rounds_create,
        "converge_seconds": round(settle_create, 4),
        "resource_versions_to_converge": rv_after_settle - rv_after_create,
        "objects": counts,
        "objects_total": sum(counts.values()),
        "approx_cache_bytes": footprint,
        "approx_bytes_per_cluster": round(footprint / args.clusters, 1),
        "scale_patch_resource_versions": rv_after_patch - rv_before_scale,
        "scale_reconcile_rounds": rounds_scale,
        "scale_reconcile_seconds": round(settle_scale, 4),
        "scale_writes": rv_after_scale - rv_after_patch,
        "delete_rounds": rounds_delete,
        "delete_seconds": round(settle_delete, 4),
        "remaining_objects": remaining,
    }

    if args.json:
        print(json.dumps(summary, indent=2))
        return 0

    print("MiniKubeRay control-plane scale benchmark")
    print(f"  clusters={args.clusters} workers/cluster={args.workers}")
    print()
    print(f"  create (store only)     : {create_elapsed:8.3f}s")
    print(f"  converge                : {settle_create:8.3f}s / {rounds_create} rounds")
    print(f"  resourceVersion writes  : {rv_after_settle - rv_after_create}")
    print(f"  objects                 : {sum(counts.values())} ({counts})")
    print(f"  approx cached JSON      : {footprint / 1024 / 1024:.2f} MiB "
          f"({footprint / args.clusters / 1024:.1f} KiB/cluster)")
    print()
    print(f"  scale-up patch          : {rv_after_patch - rv_before_scale} writes")
    print(f"  scale-up reconcile      : {settle_scale:8.3f}s / {rounds_scale} rounds")
    print(f"  scale-up writes         : {rv_after_scale - rv_after_patch}")
    print()
    print(f"  delete + GC             : {settle_delete:8.3f}s / {rounds_delete} rounds")
    print(f"  remaining objects       : {sum(remaining.values())} ({remaining})")
    print()
    print("  Note: wall-clock numbers depend on the machine; object counts and")
    print("  resourceVersion deltas are deterministic for a given input.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
