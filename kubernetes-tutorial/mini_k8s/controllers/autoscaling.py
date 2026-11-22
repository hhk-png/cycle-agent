"""HorizontalPodAutoscaler and a metrics-server-like simulation."""

from __future__ import annotations

import math
import time

from mini_k8s.apimachinery.meta import deep_copy, now_iso
from mini_k8s.apimachinery.quantity import parse_quantity
from mini_k8s.controllers.base import Controller, condition, set_condition

HPA_GVR = ("autoscaling", "v2", "horizontalpodautoscalers")
DEPLOYMENT_GVR = ("apps", "v1", "deployments")
STS_GVR = ("apps", "v1", "statefulsets")
RS_GVR = ("apps", "v1", "replicasets")
POD_GVR = ("", "v1", "pods")
POD_METRICS_GVR = ("metrics.k8s.io", "v1beta1", "pods")
NODE_METRICS_GVR = ("metrics.k8s.io", "v1beta1", "nodes")
NODE_GVR = ("", "v1", "nodes")


def simulated_cpu_millicores(pod: dict) -> float:
    """Deterministic CPU usage: explicit annotation, else 60% of request."""
    annotation = pod.get("metadata", {}).get("annotations", {}).get("mini-k8s.io/simulate.cpu-usage")
    if annotation:
        try:
            text = str(annotation)
            return float(text[:-1]) if text.endswith("m") else float(text) * 1000
        except ValueError:
            pass
    request = 0.0
    for container in pod.get("spec", {}).get("containers", []):
        request += parse_quantity(container.get("resources", {}).get("requests", {}).get("cpu", "0"))
    return (request * 1000) * 0.6 if request else 100.0


def simulated_memory_bytes(pod: dict) -> float:
    annotation = pod.get("metadata", {}).get("annotations", {}).get("mini-k8s.io/simulate.memory-usage")
    if annotation:
        try:
            return parse_quantity(annotation)
        except ValueError:
            pass
    request = 0.0
    for container in pod.get("spec", {}).get("containers", []):
        request += parse_quantity(container.get("resources", {}).get("requests", {}).get("memory", "0"))
    return request * 0.7 if request else 64 * 1024 * 1024


def _split_usage(pod: dict, total: float, resource: str) -> list[int]:
    """Split a Pod-level usage across containers without changing the total.

    metrics-server reports per-container usage; HPA sums those values.  Giving
    every container the whole Pod usage would double-count multi-container
    Pods, so allocate proportionally to each container's request (or evenly
    when no container requests the resource) and put the rounding remainder on
    the last container.
    """
    containers = pod.get("spec", {}).get("containers", [])
    if not containers:
        return []
    weights = [parse_quantity(c.get("resources", {}).get("requests", {}).get(resource, "0"))
               for c in containers]
    if sum(weights) <= 0:
        weights = [1.0] * len(containers)
    total_int = int(total)
    shares: list[int] = []
    allocated = 0
    for index, weight in enumerate(weights):
        if index == len(weights) - 1:
            shares.append(total_int - allocated)
        else:
            share = int(total_int * weight / sum(weights))
            shares.append(share)
            allocated += share
    return shares


class MetricsController(Controller):
    """Publishes PodMetrics/NodeMetrics, the API a metrics server would serve."""

    name = "metrics-controller"

    def reconcile(self) -> None:
        pods = self.store.list(POD_GVR)[0]
        live_pods = [p for p in pods if p.get("status", {}).get("phase") == "Running"]
        by_node: dict[str, list[dict]] = {}
        for pod in live_pods:
            namespace = pod["metadata"]["namespace"]
            name = pod["metadata"]["name"]
            cpu = simulated_cpu_millicores(pod)
            memory = simulated_memory_bytes(pod)
            containers = [
                {"name": container["name"],
                 "usage": {"cpu": f"{cpu_share}m", "memory": str(memory_share)}}
                for container, cpu_share, memory_share in zip(
                    pod.get("spec", {}).get("containers", []),
                    _split_usage(pod, cpu, "cpu"),
                    _split_usage(pod, memory, "memory"),
                )
            ]
            body = {
                "apiVersion": "metrics.k8s.io/v1beta1", "kind": "PodMetrics",
                "metadata": {"name": name, "namespace": namespace, "labels": pod["metadata"].get("labels", {})},
                "timestamp": now_iso(), "window": "30s", "containers": containers,
            }
            self._upsert(POD_METRICS_GVR, namespace, name, body, "PodMetrics")
            node_name = pod.get("spec", {}).get("nodeName")
            if node_name:
                by_node.setdefault(node_name, []).append(pod)
        for node in self.store.list(NODE_GVR)[0]:
            name = node["metadata"]["name"]
            pods_on_node = by_node.get(name, [])
            cpu = sum(simulated_cpu_millicores(p) for p in pods_on_node)
            memory = sum(simulated_memory_bytes(p) for p in pods_on_node)
            body = {
                "apiVersion": "metrics.k8s.io/v1beta1", "kind": "NodeMetrics",
                "metadata": {"name": name, "labels": node["metadata"].get("labels", {})},
                "timestamp": now_iso(), "window": "30s",
                "usage": {"cpu": f"{int(cpu)}m", "memory": str(int(memory))},
            }
            self._upsert(NODE_METRICS_GVR, "", name, body, "NodeMetrics")

    def _upsert(self, gvr, namespace: str, name: str, body: dict, kind: str) -> None:
        existing = self.store.get(gvr, namespace, name)
        try:
            if existing is None:
                self.store.create(gvr, body, kind=kind, namespace=namespace)
            else:
                body["metadata"]["resourceVersion"] = existing["metadata"]["resourceVersion"]
                if body != existing:
                    self.store.update(gvr, body, kind=kind, namespace=namespace)
        except Exception:
            pass


class HPAController(Controller):
    name = "horizontalpodautoscaler-controller"

    def __init__(self, store, *, interval: float = 0.25):
        super().__init__(store, interval=interval)

    def reconcile(self) -> None:
        for hpa in self.store.list(HPA_GVR)[0]:
            if hpa["metadata"].get("deletionTimestamp"):
                continue
            self._reconcile(hpa)

    def _reconcile(self, hpa: dict) -> None:
        namespace = hpa["metadata"]["namespace"]
        spec = hpa.get("spec", {})
        target_ref = spec.get("scaleTargetRef", {})
        kind = target_ref.get("kind")
        gvr = {"Deployment": DEPLOYMENT_GVR, "StatefulSet": STS_GVR, "ReplicaSet": RS_GVR}.get(kind)
        if not gvr:
            self._fail(hpa, "InvalidTarget", f"unsupported scale target kind {kind!r}")
            return
        target = self.store.get(gvr, namespace, target_ref.get("name", ""))
        if target is None:
            self._fail(hpa, "FailedGetScale", f"target {kind}/{target_ref.get('name')} not found")
            return
        current = int(target.get("spec", {}).get("replicas", 0))
        selector = target.get("spec", {}).get("selector", {}).get("matchLabels", {})
        pods = [p for p in self.store.list(POD_GVR, namespace)[0]
                if all(p["metadata"].get("labels", {}).get(k) == v for k, v in selector.items())
                and p.get("status", {}).get("phase") == "Running"]
        metrics = spec.get("metrics", [])
        current_metrics: list[dict] = []
        desired = current
        if pods and metrics:
            # HPA computes a desired replica count per metric and takes the
            # maximum.  Ratios within the default 10% tolerance keep the
            # current count, which is what prevents flapping around the target.
            candidates: list[int] = []
            for metric in metrics:
                metric_type = metric.get("type")
                if metric_type == "Resource":
                    resource = metric.get("resource", {})
                    resource_name = resource.get("name", "cpu")
                    if resource_name == "cpu":
                        requests = sum(parse_quantity(c.get("resources", {}).get("requests", {}).get("cpu", "0"))
                                       for p in pods for c in p.get("spec", {}).get("containers", []))
                        usages = self._pod_usage_millicores(pods)
                        average_util = ((sum(usages) / 1000.0) / requests * 100.0) if requests else 0.0
                    else:
                        requests = sum(parse_quantity(c.get("resources", {}).get("requests", {}).get("memory", "0"))
                                       for p in pods for c in p.get("spec", {}).get("containers", []))
                        usages = self._pod_usage_bytes(pods)
                        average_util = ((sum(usages) / requests) * 100.0) if requests else 0.0
                    target_spec = resource.get("target", {})
                    target_util = float(target_spec.get("averageUtilization", 50))
                    if average_util <= 0 or target_util <= 0:
                        candidate = current
                    else:
                        ratio = average_util / target_util
                        candidate = current if abs(ratio - 1.0) <= 0.1 else max(1, math.ceil(current * ratio))
                    average_raw = int(sum(usages) / max(1, len(pods)))
                    average_value = (f"{int(average_raw / (1024 * 1024))}Mi"
                                     if resource_name == "memory" else f"{average_raw}m")
                    current_metrics.append({"type": "Resource", "resource": {
                        "name": resource_name, "current": {"averageUtilization": int(round(average_util)),
                                                           "averageValue": average_value}}})
                elif metric_type == "Pods":
                    metric_spec = metric.get("pods", {})
                    target_avg = parse_quantity(metric_spec.get("target", {}).get("averageValue", "0"))
                    usages = self._pod_usage_millicores(pods)
                    average = sum(usages) / len(usages)
                    if target_avg <= 0:
                        candidate = current
                    else:
                        ratio = average / target_avg
                        candidate = current if abs(ratio - 1.0) <= 0.1 else max(1, math.ceil(current * ratio))
                    current_metrics.append({"type": "Pods", "pods": {
                        "metric": metric_spec.get("metric", {}),
                        "current": {"averageValue": f"{int(average)}m"}}})
                else:
                    # Object/External metrics need an adapter; leave the
                    # current count unchanged and report the metric as seen.
                    candidate = current
                    current_metrics.append(metric)
                candidates.append(candidate)
            desired = max(candidates) if candidates else current
        minimum = int(spec.get("minReplicas", 1))
        maximum = int(spec.get("maxReplicas", 1))
        limited = desired < minimum or desired > maximum
        desired = max(minimum, min(maximum, desired))
        if desired != current:
            updated = deep_copy(target)
            updated["spec"]["replicas"] = desired
            try:
                self.store.update(gvr, updated, kind=kind, namespace=namespace)
            except Exception:
                pass
        conditions = hpa.get("status", {}).get("conditions", [])
        conditions = set_condition(conditions, condition("AbleToScale", True, "SucceededGetScale",
                                                         f"the HPA controller was able to get the target's current scale"))
        conditions = set_condition(conditions, condition("ScalingActive", True, "ValidMetricFound",
                                                         f"the HPA was able to successfully calculate a replica count from metrics"))
        if limited:
            conditions = set_condition(conditions, condition(
                "ScalingLimited", True, "TooManyReplicas" if desired >= maximum else "TooFewReplicas",
                f"the desired replica count was limited to {desired} by min/max",
            ))
        else:
            conditions = [c for c in conditions if c.get("type") != "ScalingLimited"]
        status = {
            "observedGeneration": hpa["metadata"].get("generation", 1),
            "lastScaleTime": (now_iso() if desired != current else hpa.get("status", {}).get("lastScaleTime")),
            "currentReplicas": current,
            "desiredReplicas": desired,
            "currentMetrics": current_metrics,
            "conditions": conditions,
        }
        self.update_status(HPA_GVR, hpa, status)

    def _pod_usage_millicores(self, pods: list[dict]) -> list[float]:
        result = []
        for pod in pods:
            metric = self.store.get(POD_METRICS_GVR, pod["metadata"]["namespace"], pod["metadata"]["name"])
            if metric:
                total = 0.0
                for container in metric.get("containers", []):
                    total += parse_quantity(container.get("usage", {}).get("cpu", "0"))
                result.append(total)
            else:
                result.append(simulated_cpu_millicores(pod))
        return result

    def _pod_usage_bytes(self, pods: list[dict]) -> list[float]:
        result = []
        for pod in pods:
            metric = self.store.get(POD_METRICS_GVR, pod["metadata"]["namespace"], pod["metadata"]["name"])
            if metric:
                total = 0.0
                for container in metric.get("containers", []):
                    total += parse_quantity(container.get("usage", {}).get("memory", "0"))
                result.append(total)
            else:
                result.append(simulated_memory_bytes(pod))
        return result

    def _fail(self, hpa: dict, reason: str, message: str) -> None:
        self.update_status(HPA_GVR, hpa, {
            "currentReplicas": 0, "desiredReplicas": 0, "currentMetrics": [],
            "conditions": [condition("AbleToScale", False, reason, message)],
        })
