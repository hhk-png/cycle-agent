"""RayCluster-specific naming, sizing and status helpers."""

from __future__ import annotations

from .. import constants as C
from ..util import clamp, resource_quantity
from .meta import is_condition_true, name as obj_name, namespace as obj_namespace


def group_desired_pods(group: dict) -> int:
    """KubeRay's GetWorkerGroupDesiredReplicas: clamp, then multiply hosts."""
    if group.get("suspend"):
        return 0
    min_replicas = int(group.get("minReplicas", 0) or 0)
    max_replicas = int(group.get("maxReplicas", C.MAX_INT32) or C.MAX_INT32)
    replicas = group.get("replicas")
    if replicas is None or int(replicas) < min_replicas:
        replicas = min_replicas
    else:
        replicas = int(replicas)
    replicas = clamp(replicas, min_replicas, max_replicas)
    hosts = max(1, int(group.get("numOfHosts", 1) or 1))
    return replicas * hosts


def group_replicas(group: dict) -> int:
    """Replica count without the numOfHosts multiplier."""
    hosts = max(1, int(group.get("numOfHosts", 1) or 1))
    return group_desired_pods(group) // hosts


def desired_worker_replicas(cluster_or_spec: dict) -> int:
    spec = cluster_or_spec.get("spec", cluster_or_spec)
    return sum(group_desired_pods(group) for group in spec.get("workerGroupSpecs") or [])


def min_worker_replicas(spec: dict) -> int:
    total = 0
    for group in spec.get("workerGroupSpecs") or []:
        if group.get("suspend"):
            continue
        total += int(group.get("minReplicas", 0) or 0) * max(1, int(group.get("numOfHosts", 1) or 1))
    return total


def max_worker_replicas(spec: dict) -> int:
    total = 0
    for group in spec.get("workerGroupSpecs") or []:
        if group.get("suspend"):
            continue
        total += int(group.get("maxReplicas", C.MAX_INT32) or C.MAX_INT32) * max(1, int(group.get("numOfHosts", 1) or 1))
    return total


def worker_group_capacity(group: dict) -> dict[str, float]:
    """Requested resources of one worker Pod (the scheduling footprint).

    This includes sidecars: it is what the scheduler and the status counters
    need.  Use :func:`ray_group_capacity` when the question is "how much can
    Ray schedule on a new Pod".
    """
    template = group.get("template") or {}
    containers = (template.get("spec") or {}).get("containers") or []
    totals: dict[str, float] = {"cpu": 0.0, "memory": 0.0, "nvidia.com/gpu": 0.0}
    for container in containers:
        requests = container.get("resources", {}).get("requests", {})
        for resource in list(totals):
            totals[resource] += resource_quantity(requests, resource)
        for resource, value in requests.items():
            if resource not in totals:
                totals[resource] = resource_quantity({resource: value}, resource)
    return totals


def ray_group_capacity(group: dict) -> dict[str, float]:
    """Requested resources of the Ray container only.

    Ray's scheduler sees the Ray container, not the sidecars.  The autoscaler
    must use this number when deciding how many replicas are needed; counting a
    log shipper as Ray capacity would under-provision the cluster.
    """
    from ..util import get_ray_container

    template = group.get("template") or {}
    container = get_ray_container(template.get("spec") or {}) or {}
    requests = (container.get("resources") or {}).get("requests") or {}
    totals: dict[str, float] = {"cpu": 0.0, "memory": 0.0, "nvidia.com/gpu": 0.0}
    for resource in totals:
        totals[resource] = resource_quantity(requests, resource)
    for resource, value in requests.items():
        if resource not in totals:
            totals[resource] = resource_quantity({resource: value}, resource)
    return totals


def head_service_name(cluster: dict) -> str:
    spec = cluster.get("spec") or {}
    custom = ((spec.get("headGroupSpec") or {}).get("headService") or {})
    custom_name = (custom.get("metadata") or {}).get("name")
    return custom_name or f"{obj_name(cluster)}-head-svc"


def serve_service_name(cluster_or_name: dict | str) -> str:
    name = obj_name(cluster_or_name) if isinstance(cluster_or_name, dict) else cluster_or_name
    return f"{name}-serve-svc"


def headless_service_name(cluster: dict) -> str:
    return f"{obj_name(cluster)}-headless"


def head_fqdn(cluster: dict, domain: str = C.DEFAULT_CLUSTER_DOMAIN) -> str:
    return f"{head_service_name(cluster)}.{obj_namespace(cluster)}.svc.{domain}"


def pod_name(
    cluster: dict,
    node_type: str,
    group: str | None = None,
    index: int | None = None,
    *,
    host_index: int | None = None,
) -> str:
    base = obj_name(cluster)
    if node_type == C.NODE_TYPE_HEAD:
        return f"{base}-head"
    group = group or "worker"
    if index is None:
        return f"{base}-{group}"
    if host_index is not None:
        return f"{base}-{group}-{index}-{host_index}"
    return f"{base}-{group}-{index}"


def pod_labels(cluster: dict, node_type: str, group: str, extra: dict | None = None) -> dict:
    labels = {
        C.LABEL_IS_RAY_NODE: "yes",
        C.LABEL_CLUSTER: obj_name(cluster),
        C.LABEL_NODE_TYPE: node_type,
        C.LABEL_GROUP: group,
        C.LABEL_IDENTIFIER: f"{obj_name(cluster)}-{node_type}"[:63],
        C.LABEL_APP_NAME: "kuberay",
        C.LABEL_APP_CREATED_BY: "kuberay-operator",
    }
    for key, value in (extra or {}).items():
        if key in (C.LABEL_CLUSTER, C.LABEL_NODE_TYPE, C.LABEL_GROUP):
            continue
        labels[key] = value
    return labels


def cluster_phase(cluster: dict, pods: list[dict]) -> str:
    from .core import pod_is_ready

    status = cluster.get("status") or {}
    if is_condition_true(status, C.COND_SUSPENDED):
        return "suspended"
    head = [
        pod
        for pod in pods
        if (pod.get("metadata", {}).get("labels") or {}).get(C.LABEL_NODE_TYPE) == C.NODE_TYPE_HEAD
        and not pod.get("metadata", {}).get("deletionTimestamp")
    ]
    if not head or not pod_is_ready(head[0]):
        return ""
    desired = desired_worker_replicas(cluster)
    workers = [
        pod
        for pod in pods
        if (pod.get("metadata", {}).get("labels") or {}).get(C.LABEL_NODE_TYPE) == C.NODE_TYPE_WORKER
        and not pod.get("metadata", {}).get("deletionTimestamp")
    ]
    if desired == 0:
        return "ready"
    ready = sum(1 for pod in workers if pod_is_ready(pod))
    return "ready" if ready >= desired else ""


def condition_status(cluster: dict) -> dict:
    return cluster.get("status") or {}


def endpoint_map(cluster: dict, service_ip: str | None = None) -> dict[str, str]:
    service_name = head_service_name(cluster)
    namespace = obj_namespace(cluster)
    host = service_ip or f"{service_name}.{namespace}.svc.{C.DEFAULT_CLUSTER_DOMAIN}"
    return {
        "client": f"{host}:{C.PORT_CLIENT}",
        "dashboard": f"{host}:{C.PORT_DASHBOARD}",
        "metrics": f"{host}:{C.PORT_METRICS}",
        "serve": f"{host}:{C.PORT_SERVE}",
        "gcs-server": f"{host}:{C.PORT_GCS}",
    }


def total_requested(pods: list[dict], resource: str) -> float:
    from .core import pod_requested_resources

    return sum(pod_requested_resources(pod).get(resource, 0.0) for pod in pods if not pod.get("metadata", {}).get("deletionTimestamp"))
