#!/usr/bin/env python3
"""An end-to-end observability lab: spans, RED metrics, SLO and error budgets.

Run from the tutorial root:

    python examples/observability_lab.py

The lab uses the real MiniKubeRay operator with a deterministic clock:

1. create a RayCluster and a RayJob;
2. settle the controllers and collect the spans emitted by every reconcile;
3. render one trace as a tree (the same shape an OpenTelemetry backend shows);
4. compute RED metrics and an SLO report from spans plus a synthetic request
   sample;
5. delete the cluster and show that the History Server session survives.

The synthetic request sample is explicit: an SLO needs a denominator, and a
controller's reconcile count is not the same thing as user traffic.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from minikuberay import constants as C
from minikuberay.clock import ManualClock
from minikuberay.controllers.base import Operator, OperatorConfig
from minikuberay.models.meta import new_object
from minikuberay.tracing import span_tree


def cluster_spec() -> dict:
    return {
        "rayVersion": "2.59.0",
        "headGroupSpec": {
            "rayStartParams": {},
            "template": {
                "spec": {
                    "containers": [
                        {
                            "name": "ray",
                            "image": "rayproject/ray:2.59.0",
                            "resources": {"requests": {"cpu": "2", "memory": "4Gi"}},
                        }
                    ]
                }
            },
        },
        "workerGroupSpecs": [
            {
                "groupName": "workers",
                "replicas": 1,
                "minReplicas": 0,
                "maxReplicas": 4,
                "rayStartParams": {},
                "template": {
                    "spec": {
                        "containers": [
                            {
                                "name": "ray",
                                "image": "rayproject/ray:2.59.0",
                                "resources": {"requests": {"cpu": "2", "memory": "4Gi"}},
                            }
                        ]
                    }
                },
            }
        ],
    }


def job_spec() -> dict:
    return {
        "entrypoint": "python -c \"print('observability')\"",
        "submissionMode": "K8sJobMode",
        "shutdownAfterJobFinishes": False,
        "backoffLimit": 0,
        "rayClusterSpec": cluster_spec(),
    }


def p95(values: list[float]) -> float:
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1)))))
    return ordered[index]


def slo_report(observed_error_rate: float, latencies_ms: list[float]) -> dict:
    window_minutes = 30 * 24 * 60
    availability_slo = 0.999
    allowed_error_rate = 1 - availability_slo
    budget_minutes = window_minutes * allowed_error_rate
    bad_minutes = window_minutes * observed_error_rate
    burn_rate = observed_error_rate / allowed_error_rate
    latency_p95 = p95(latencies_ms)
    latency_ok = latency_p95 <= 2000
    return {
        "window_minutes": window_minutes,
        "availability_slo": availability_slo,
        "allowed_error_rate": allowed_error_rate,
        "budget_minutes": round(budget_minutes, 2),
        "bad_minutes": round(bad_minutes, 2),
        "budget_consumed": round(bad_minutes / budget_minutes, 3),
        "burn_rate": round(burn_rate, 3),
        "latency_p95_ms": latency_p95,
        "latency_ok": latency_ok,
    }


def main() -> int:
    clock = ManualClock()
    operator = Operator(clock=clock, config=OperatorConfig())
    operator.store.create(
        C.KIND_RAYCLUSTER,
        new_object(C.RAY_API_VERSION, C.KIND_RAYCLUSTER, "obs", "default", spec=cluster_spec()),
        "default",
    )
    operator.store.create(
        C.KIND_RAYJOB,
        new_object(C.RAY_API_VERSION, C.KIND_RAYJOB, "obs-job", "default", spec=job_spec()),
        "default",
    )
    rounds = operator.settle(max_rounds=80, advance=0.25)

    spans = operator.tracer.spans(limit=5000)
    stats = operator.tracer.stats()
    traces = operator.tracer.traces(limit=50)
    assert spans, "the operator must emit spans"
    assert rounds > 0

    print("== observability lab ==")
    print(f"settled in {rounds} rounds; spans={stats['spans']} traces={len(traces)}")
    print(f"RED: errors={stats['errors']} error_rate={stats['error_rate']:.4f} "
          f"p50={stats['p50_ms']}ms p95={stats['p95_ms']}ms p99={stats['p99_ms']}ms")

    # Pick a trace that contains the operator round plus at least one reconcile.
    trace = next((item for item in traces if item["span_count"] >= 3), traces[-1])
    print(f"\n-- trace {trace['trace_id'][:12]} ({trace['span_count']} spans) --")
    for line in span_tree(trace["spans"]):
        print("  " + line)

    # SLO: 99.9% availability, 30-day window, and a synthetic user sample.
    observed_error_rate = 0.0012
    latencies = [120 + (index % 50) * 7 for index in range(1000)]
    report = slo_report(observed_error_rate, latencies)
    print("\n-- SLO report (30-day availability 99.9%) --")
    print(f"error budget      : {report['budget_minutes']} min")
    print(f"bad minutes       : {report['bad_minutes']} min "
          f"({report['budget_consumed'] * 100:.1f}% consumed)")
    print(f"burn rate         : {report['burn_rate']}x")
    print(f"latency p95       : {report['latency_p95_ms']} ms (<= 2000: {report['latency_ok']})")

    # Multi-window burn: a fast burn is actionable; a slow burn is a trend.
    windows = [
        ("1h", 0.0040, 14.4),
        ("6h", 0.0015, 6.0),
        ("3d", 0.0011, 1.0),
    ]
    print("\n-- multi-window burn --")
    for window, rate, threshold in windows:
        burn = rate / 0.001
        print(f"  {window:<3} error_rate={rate:.4%} burn={burn:.1f}x "
              f"threshold={threshold}x -> {'ALERT' if burn >= threshold else 'ok'}")

    # History must survive deletion.
    operator.store.delete(C.KIND_RAYCLUSTER, "obs", "default")
    operator.settle(max_rounds=30)
    sessions = operator.history.list_sessions() if operator.history else []
    deleted = [session for session in sessions if session.get("cluster") == "obs"]
    assert deleted and deleted[0].get("deletedAt"), "history session must survive cluster deletion"

    assert stats["errors"] == 0
    assert report["budget_consumed"] > 1.0, "the sample intentionally exceeds the budget"
    assert report["burn_rate"] == 1.2
    assert report["latency_ok"] is True
    assert any(line.strip().startswith("operator.round") for line in span_tree(trace["spans"]))
    assert any("reconcile" in line for line in span_tree(trace["spans"]))

    print("\nOK: spans form a trace tree, the SLO math is reproducible, and History survives deletion.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
