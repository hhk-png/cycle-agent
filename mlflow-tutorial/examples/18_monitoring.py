"""Model monitoring: baseline, drift windows and an auditable report.

This example closes the loop that starts in chapter 43 (deploy and collect
feedback) and chapter 44 (a managed evaluation dataset): a monitor is bound to
a baseline dataset, then two batches of production-like records are evaluated.
The stable batch stays green; the shifted batch raises ALERT because tenure,
charges and plan mix all moved outside the baseline distribution.

Run from the tutorial root:

    python examples/18_monitoring.py
"""

from __future__ import annotations

import os

from _common import client, configure_fluent, print_json

import mini_mlflow as mlflow


def _baseline(count: int = 200) -> list[dict]:
    rows = []
    for index in range(count):
        rows.append(
            {
                "tenure": float(1 + index % 60),
                "monthly_charges": float(20 + (index * 7) % 80),
                "plan": "basic" if index % 3 else "pro",
                "support_calls": float(index % 6),
            }
        )
    return rows


def _stable(count: int = 200) -> list[dict]:
    # Same generator: the distribution is identical to the baseline.
    return _baseline(count)


def _drifted(count: int = 200) -> list[dict]:
    rows = []
    for index in range(count):
        rows.append(
            {
                # New customers arrive with much shorter tenure and higher
                # charges; a new plan appears that the model never saw.
                "tenure": float(1 + index % 5),
                "monthly_charges": float(80 + (index * 11) % 60),
                "plan": "enterprise" if index % 2 else "pro",
                "support_calls": float(3 + index % 10),
            }
        )
    return rows


def main() -> None:
    active = client()
    configure_fluent(active)
    suffix = os.environ.get("MINI_MLFLOW_DEMO_SUFFIX") or "demo"
    dataset = None
    monitor = None
    try:
        dataset = active.create_dataset(
            f"churn-monitor-baseline-{suffix}",
            [{"inputs": row, "tags": {"slice": "baseline"}} for row in _baseline()],
            source="s3://warehouse/churn/2026-11-01.parquet",
            source_type="s3",
            tags={"owner": "risk", "stage": "monitoring-baseline"},
        )
        monitor = active.create_monitor(
            f"churn-monitor-{suffix}",
            [],
            {
                "tenure": "numeric",
                "monthly_charges": "numeric",
                "plan": "categorical",
                "support_calls": "numeric",
            },
            model_name="churn",
            model_version="3",
            dataset_id=dataset.dataset_id,
            tags={"owner": "risk", "alert_channel": "#ml-oncall"},
        )

        stable = active.record_monitor_window(
            monitor.monitor_id, _stable(), window_start=1_000, window_end=2_000
        )
        drifted = active.record_monitor_window(
            monitor.monitor_id, _drifted(), window_start=2_000, window_end=3_000
        )

        # Monitoring is not a separate silo: log the summary into the same
        # experiment/run graph as training and evaluation.
        with mlflow.start_run(run_name="daily-drift-check"):
            mlflow.log_param("monitor_id", monitor.monitor_id)
            mlflow.log_param("model_version", monitor.model_version)
            mlflow.log_param("baseline_digest", monitor.baseline["digest"])
            for name, metrics in drifted.metrics.items():
                if metrics.get("psi") is not None:
                    mlflow.log_metric(f"drift/{name}/psi", float(metrics["psi"]))
                if metrics.get("ks_pvalue") is not None:
                    mlflow.log_metric(
                        f"drift/{name}/ks_pvalue", float(metrics["ks_pvalue"])
                    )
            mlflow.log_metric("drift/alert_count", float(drifted.status == "ALERT"))
            mlflow.log_dict(drifted.report, "drift/report.json")

        print_json(
            {
                "monitor_id": monitor.monitor_id,
                "baseline_dataset": {
                    "dataset_id": dataset.dataset_id,
                    "digest": dataset.digest,
                    "records": dataset.record_count,
                },
                "stable": {
                    "status": stable.status,
                    "sample_count": stable.sample_count,
                    "psi": {
                        name: round(float(metrics["psi"]), 4)
                        for name, metrics in stable.metrics.items()
                    },
                },
                "drifted": {
                    "status": drifted.status,
                    "sample_count": drifted.sample_count,
                    "psi": {
                        name: round(float(metrics["psi"]), 4)
                        for name, metrics in drifted.metrics.items()
                    },
                    "reasons": drifted.report["reasons"],
                },
                "windows": len(active.list_monitor_windows(monitor.monitor_id)),
            }
        )
    finally:
        if monitor is not None:
            active.delete_monitor(monitor.monitor_id)
        if dataset is not None:
            active.delete_dataset(dataset.dataset_id)
        active.close()


if __name__ == "__main__":
    main()
