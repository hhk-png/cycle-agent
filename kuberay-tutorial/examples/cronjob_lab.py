#!/usr/bin/env python3
"""Deterministic RayCronJob lab: schedules, time zones, suspend and idempotency.

    python examples/cronjob_lab.py

RayCronJob is alpha upstream and only has four fields (schedule, timeZone,
suspend, jobTemplate).  That makes its edge cases easy to demonstrate -- and
easy to get wrong in a platform:

* a missed operator restart must not silently drop a run;
* a duplicate reconcile in the same minute must not create two RayJobs;
* ``suspend`` must not delete already-created children;
* ``timeZone`` changes the meaning of the cron expression.

The lab uses ManualClock, so every assertion is exact.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from minikuberay import constants as C
from minikuberay.clock import ManualClock
from minikuberay.controllers.base import Operator, OperatorConfig
from minikuberay.models.meta import new_object


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
                            "resources": {"requests": {"cpu": "500m", "memory": "512Mi"}},
                        }
                    ],
                }
            },
        },
        "workerGroupSpecs": [],
    }


def cronjob(name: str, schedule: str, *, timezone_name: str = "UTC") -> dict:
    return new_object(
        C.RAY_API_VERSION,
        C.KIND_RAYCRONJOB,
        name,
        "default",
        spec={
            "schedule": schedule,
            "timeZone": timezone_name,
            "jobTemplate": {
                "entrypoint": "python -c \"print('scheduled run')\"",
                "shutdownAfterJobFinishes": True,
                "rayClusterSpec": cluster_spec(),
            },
        },
    )


def children(operator: Operator, name: str) -> list[dict]:
    return [
        job
        for job in operator.store.list(C.KIND_RAYJOB, "default")
        if (job.get("metadata", {}).get("labels") or {}).get(C.LABEL_CRONJOB_NAME) == name
    ]


def run_until(operator: Operator, clock: ManualClock, seconds: int, step: int = 60) -> None:
    remaining = seconds
    while remaining > 0:
        operator.run_once()
        delta = min(step, remaining)
        clock.advance(delta)
        remaining -= delta
    operator.run_once()


def main() -> int:
    clock = ManualClock()
    config = OperatorConfig()
    config.feature_gates["RayCronJob"] = True
    operator = Operator(clock=clock, config=config)
    failures = 0

    print(f"start clock: {clock.now().isoformat()}")

    # ---------------------------------------------------------------- UTC daily
    operator.store.create(C.KIND_RAYCRONJOB, cronjob("nightly", "0 2 * * *"), "default")
    run_until(operator, clock, 3 * 3600)
    nightly = children(operator, "nightly")
    print(f"\nnightly (0 2 * * *, UTC)")
    print(f"  clock={clock.now().isoformat()} children={[job['metadata']['name'] for job in nightly]}")
    if len(nightly) != 1:
        print("  expected exactly one child after 02:00 UTC")
        failures += 1

    # A duplicate reconcile in the same minute must be idempotent.
    before = len(nightly)
    operator.run_once()
    operator.run_once()
    after = len(children(operator, "nightly"))
    print(f"  duplicate reconciles: {before} -> {after} child(ren)")
    if after != before:
        print("  expected the same-minute reconcile to be idempotent")
        failures += 1

    # ------------------------------------------------------- suspended window
    cron = operator.store.get(C.KIND_RAYCRONJOB, "nightly", "default")
    cron["spec"]["suspend"] = True
    operator.store.update(C.KIND_RAYCRONJOB, cron)
    run_until(operator, clock, 24 * 3600, step=3600)
    suspended_children = children(operator, "nightly")
    print(f"\nsuspended for 24h: children={len(suspended_children)}")
    if len(suspended_children) != 1:
        print("  expected suspend to stop scheduling without deleting children")
        failures += 1

    # Resume; the next 02:00 should schedule again.  Missing one day is
    # acceptable (upstream also does not promise backfill of every missed run),
    # so assert only that a new child appears within the next 48h.
    cron = operator.store.get(C.KIND_RAYCRONJOB, "nightly", "default")
    cron["spec"]["suspend"] = False
    operator.store.update(C.KIND_RAYCRONJOB, cron)
    run_until(operator, clock, 48 * 3600, step=3600)
    resumed = children(operator, "nightly")
    print(f"after resume + 48h: children={len(resumed)}")
    if len(resumed) <= len(suspended_children):
        print("  expected a new child after resume")
        failures += 1

    # --------------------------------------------------------- time zone math
    # Pin the clock so the arithmetic is obvious: 2026-01-05 00:00 UTC is
    # 08:00 in Asia/Shanghai; the next 09:00 local occurrence is 01:00 UTC.
    clock.set(datetime(2026, 1, 5, 0, 0, tzinfo=timezone.utc))
    shanghai = cronjob("morning-cn", "0 9 * * *", timezone_name="Asia/Shanghai")
    operator.store.create(C.KIND_RAYCRONJOB, shanghai, "default")
    run_until(operator, clock, 2 * 3600)
    cn_children = children(operator, "morning-cn")
    print(f"\nmorning-cn (0 9 * * *, Asia/Shanghai)")
    print(f"  clock={clock.now().isoformat()} children={[job['metadata']['name'] for job in cn_children]}")
    if len(cn_children) != 1:
        print("  expected one child at 01:00 UTC (= 09:00 Asia/Shanghai)")
        failures += 1

    print("\n== summary ==")
    for name in ("nightly", "morning-cn"):
        cron = operator.store.get(C.KIND_RAYCRONJOB, name, "default")
        print(f"{name:<12} lastScheduleTime={cron.get('status', {}).get('lastScheduleTime', '-')}")

    if failures:
        print(f"\nFAILED: {failures} check(s)")
        return 1
    print("\nOK: cron scheduling, time zone, suspend and idempotency behaved as specified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
