"""Dependency-free Prometheus text exposition.

The metric names follow the public KubeRay metric families where practical:

  kuberay_operator_reconcile_total
  kuberay_operator_reconcile_errors_total
  kuberay_operator_reconcile_duration_seconds
  kuberay_raycluster_ready
  kuberay_rayjob_status
  kuberay_rayservice_ready
  kuberay_autoscaler_actions_total
"""

from __future__ import annotations

import math
import threading
from dataclasses import dataclass, field


def _escape(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _labels(labels: dict[str, str] | None) -> str:
    if not labels:
        return ""
    body = ",".join(f'{key}="{_escape(value)}"' for key, value in sorted(labels.items()))
    return "{" + body + "}"


@dataclass
class _Metric:
    name: str
    help: str
    kind: str
    samples: dict[tuple[tuple[str, str], ...], float] = field(default_factory=dict)
    buckets: tuple[float, ...] = ()

    def key(self, labels: dict[str, str] | None) -> tuple[tuple[str, str], ...]:
        return tuple(sorted((labels or {}).items()))


class MetricsRegistry:
    def __init__(self):
        self._lock = threading.Lock()
        self._metrics: dict[str, _Metric] = {}

    def _metric(self, name: str, help_text: str, kind: str, buckets: tuple[float, ...] = ()) -> _Metric:
        if name not in self._metrics:
            self._metrics[name] = _Metric(name, help_text, kind, buckets=buckets)
        return self._metrics[name]

    def inc(self, name: str, help_text: str = "", labels: dict[str, str] | None = None, value: float = 1.0) -> None:
        with self._lock:
            metric = self._metric(name, help_text or name, "counter")
            key = metric.key(labels)
            metric.samples[key] = metric.samples.get(key, 0.0) + value

    def set(self, name: str, value: float, help_text: str = "", labels: dict[str, str] | None = None) -> None:
        with self._lock:
            metric = self._metric(name, help_text or name, "gauge")
            metric.samples[metric.key(labels)] = float(value)

    def observe(self, name: str, value: float, help_text: str = "", labels: dict[str, str] | None = None, buckets: tuple[float, ...] = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)) -> None:
        with self._lock:
            metric = self._metric(name, help_text or name, "histogram", buckets)
            key = metric.key(labels)
            metric.samples[key] = metric.samples.get(key, 0.0) + 1.0
            # Store sum/count/bucket sample keys using synthetic label keys.
            for bound in metric.buckets:
                if value <= bound:
                    bucket_key = metric.key({**(labels or {}), "le": str(bound)})
                    metric.samples[bucket_key] = metric.samples.get(bucket_key, 0.0) + 1.0
            bucket_key = metric.key({**(labels or {}), "le": "+Inf"})
            metric.samples[bucket_key] = metric.samples.get(bucket_key, 0.0) + 1.0
            metric.samples[metric.key({**(labels or {}), "_sum": "true"})] = metric.samples.get(metric.key({**(labels or {}), "_sum": "true"}), 0.0) + value

    def render(self) -> str:
        with self._lock:
            lines: list[str] = []
            for name in sorted(self._metrics):
                metric = self._metrics[name]
                lines.append(f"# HELP {name} {metric.help}")
                lines.append(f"# TYPE {name} {metric.kind}")
                for key, value in sorted(metric.samples.items()):
                    labels = dict(key)
                    # Histograms are rendered once below as _bucket/_sum/_count;
                    # their base sample is only the internal counter.
                    if metric.kind == "histogram":
                        continue
                    rendered = _labels(labels)
                    lines.append(f"{name}{rendered} {_format_float(value)}")
                if metric.kind == "histogram":
                    for key, value in sorted(metric.samples.items()):
                        labels = dict(key)
                        if labels.get("le") is not None:
                            base = {k: v for k, v in labels.items() if k != "le"}
                            lines.append(f"{name}_bucket{_labels({**base, 'le': labels['le']})} {_format_float(value)}")
                        elif labels.get("_sum") == "true":
                            base = {k: v for k, v in labels.items() if k != "_sum"}
                            # Count and sum share the same base label set.
                            count = metric.samples.get(metric.key(base), 0.0)
                            lines.append(f"{name}_sum{_labels(base)} {_format_float(value)}")
                            lines.append(f"{name}_count{_labels(base)} {_format_float(count)}")
            return "\n".join(lines) + "\n"


def _format_float(value: float) -> str:
    if math.isnan(value):
        return "NaN"
    if math.isinf(value):
        return "+Inf" if value > 0 else "-Inf"
    if float(value).is_integer():
        return str(int(value))
    return f"{value:.9g}"


class OperatorMetrics:
    """Convenience wrapper for the metric families an operator should expose."""

    def __init__(self, registry: MetricsRegistry | None = None):
        self.registry = registry or MetricsRegistry()

    def reconcile(self, controller: str, result: str, duration: float) -> None:
        self.registry.inc("kuberay_operator_reconcile_total", "Total reconcile attempts", {"controller": controller, "result": result})
        self.registry.observe("kuberay_operator_reconcile_duration_seconds", duration, "Reconcile duration", {"controller": controller})
        if result == "error":
            self.registry.inc("kuberay_operator_reconcile_errors_total", "Total reconcile errors", {"controller": controller})

    def cluster_ready(self, namespace: str, name: str, ready: bool) -> None:
        self.registry.set("kuberay_raycluster_ready", 1 if ready else 0, "RayCluster readiness (MiniKubeRay)", {"namespace": namespace, "raycluster": name})
        self.registry.set("kuberay_cluster_info", 1, "RayCluster info", {"namespace": namespace, "raycluster": name})
        self.registry.set("kuberay_cluster_condition_provisioned", 1 if ready else 0, "RayClusterProvisioned condition", {"namespace": namespace, "raycluster": name, "condition": "RayClusterProvisioned"})

    def job_status(self, namespace: str, name: str, status: str, deployment_status: str = "") -> None:
        for candidate in ("PENDING", "RUNNING", "SUCCEEDED", "FAILED", "STOPPED", ""):
            self.registry.set(
                "kuberay_rayjob_status",
                1 if candidate == status else 0,
                "RayJob status (one-hot, MiniKubeRay)",
                {"namespace": namespace, "rayjob": name, "status": candidate or "NEW"},
            )
        self.registry.set("kuberay_job_info", 1, "RayJob info", {"namespace": namespace, "rayjob": name})
        for candidate in ("Initializing", "Running", "Complete", "Failed", "Suspended", "Retrying", "Waiting", ""):
            self.registry.set(
                "kuberay_job_deployment_status",
                1 if candidate == deployment_status else 0,
                "RayJob deployment status (one-hot)",
                {"namespace": namespace, "rayjob": name, "status": candidate or "New"},
            )

    def service_ready(self, namespace: str, name: str, ready: bool) -> None:
        self.registry.set("kuberay_rayservice_ready", 1 if ready else 0, "RayService readiness (MiniKubeRay)", {"namespace": namespace, "rayservice": name})
        self.registry.set("kuberay_service_info", 1, "RayService info", {"namespace": namespace, "rayservice": name})
        self.registry.set("kuberay_service_condition_ready", 1 if ready else 0, "RayService Ready condition", {"namespace": namespace, "rayservice": name, "condition": "Ready"})

    def autoscaler_action(self, action: str, namespace: str, cluster: str, group: str = "") -> None:
        self.registry.inc(
            "kuberay_autoscaler_actions_total",
            "Autoscaler scale actions",
            {"action": action, "namespace": namespace, "raycluster": cluster, "group": group},
        )
