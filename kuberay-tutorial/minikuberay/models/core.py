"""Constructors and predicates for core Kubernetes objects."""

from __future__ import annotations

from .. import constants as C
from ..util import deep_copy, get_ray_container, labels_match
from .meta import new_object


def container(
    name: str,
    image: str,
    *,
    command: list[str] | None = None,
    args: list[str] | None = None,
    env: list[dict] | None = None,
    ports: list[dict] | None = None,
    resources: dict | None = None,
    volume_mounts: list[dict] | None = None,
    image_pull_policy: str = "IfNotPresent",
) -> dict:
    item = {
        "name": name,
        "image": image,
        "imagePullPolicy": image_pull_policy,
        "resources": resources or {"requests": {"cpu": "500m", "memory": "512Mi"}, "limits": {"cpu": "1", "memory": "1Gi"}},
    }
    if command:
        item["command"] = list(command)
    if args:
        item["args"] = list(args)
    if env:
        item["env"] = deep_copy(env)
    if ports:
        item["ports"] = deep_copy(ports)
    if volume_mounts:
        item["volumeMounts"] = deep_copy(volume_mounts)
    return item


def default_container_spec(image: str = C.DEFAULT_IMAGE) -> dict:
    return {
        "containers": [container("ray", image)],
        "restartPolicy": "Never",
        "terminationGracePeriodSeconds": 30,
    }


def new_pod(
    name: str,
    namespace: str,
    spec: dict,
    *,
    labels: dict | None = None,
    annotations: dict | None = None,
    owner_references: list[dict] | None = None,
    finalizers: list[str] | None = None,
) -> dict:
    return new_object(
        C.CORE_API_VERSION,
        C.KIND_POD,
        name,
        namespace,
        spec=spec,
        status={"phase": "Pending"},
        labels=labels,
        annotations=annotations,
        owner_references=owner_references,
        finalizers=finalizers,
    )


def service_port(name: str, port: int, target_port: int | None = None, protocol: str = "TCP") -> dict:
    return {
        "name": name,
        "port": port,
        "targetPort": target_port if target_port is not None else port,
        "protocol": protocol,
        "appProtocol": "tcp",
    }


def new_service(
    name: str,
    namespace: str,
    selector: dict[str, str],
    ports: list[dict],
    *,
    service_type: str = "ClusterIP",
    cluster_ip: str | None = None,
    publish_not_ready: bool = False,
    labels: dict | None = None,
    annotations: dict | None = None,
    owner_references: list[dict] | None = None,
) -> dict:
    spec = {
        "type": service_type,
        "selector": dict(selector),
        "ports": deep_copy(ports),
        "publishNotReadyAddresses": publish_not_ready,
    }
    if cluster_ip:
        spec["clusterIP"] = cluster_ip
    return new_object(
        C.CORE_API_VERSION,
        C.KIND_SERVICE,
        name,
        namespace,
        spec=spec,
        status={"loadBalancer": {}},
        labels=labels,
        annotations=annotations,
        owner_references=owner_references,
    )


def new_event(
    name: str,
    namespace: str,
    involved_object: dict,
    reason: str,
    message: str,
    *,
    event_type: str = "Normal",
    action: str = "Reconcile",
    source: str = "minikuberay-operator",
    count: int = 1,
) -> dict:
    event = new_object(
        C.CORE_API_VERSION,
        C.KIND_EVENT,
        name,
        namespace,
        spec=None,
        status=None,
        annotations=None,
    )
    event.update(
        {
            "involvedObject": {
                "apiVersion": involved_object.get("apiVersion"),
                "kind": involved_object.get("kind"),
                "name": involved_object.get("metadata", {}).get("name"),
                "namespace": involved_object.get("metadata", {}).get("namespace"),
                "uid": involved_object.get("metadata", {}).get("uid"),
                "resourceVersion": involved_object.get("metadata", {}).get("resourceVersion"),
            },
            "reason": reason,
            "message": message,
            "type": event_type,
            "action": action,
            "count": count,
            "source": {"component": source},
            "firstTimestamp": involved_object.get("metadata", {}).get("creationTimestamp"),
            "lastTimestamp": involved_object.get("metadata", {}).get("creationTimestamp"),
            "reportingComponent": source,
        }
    )
    return event


def new_job(
    name: str,
    namespace: str,
    pod_template: dict,
    *,
    backoff_limit: int = 0,
    active_deadline_seconds: int | None = None,
    labels: dict | None = None,
    annotations: dict | None = None,
    owner_references: list[dict] | None = None,
) -> dict:
    spec: dict = {
        "template": deep_copy(pod_template),
        "backoffLimit": backoff_limit,
        "completions": 1,
        "parallelism": 1,
    }
    if active_deadline_seconds is not None:
        spec["activeDeadlineSeconds"] = active_deadline_seconds
    return new_object(
        C.BATCH_API_VERSION,
        C.KIND_JOB,
        name,
        namespace,
        spec=spec,
        status={"active": 0, "succeeded": 0, "failed": 0, "conditions": []},
        labels=labels,
        annotations=annotations,
        owner_references=owner_references,
    )


def new_endpoint_slice(
    name: str,
    namespace: str,
    service_name: str,
    pods: list[dict],
    *,
    ports: list[dict] | None = None,
    labels: dict | None = None,
    owner_references: list[dict] | None = None,
) -> dict:
    endpoints = []
    for pod in pods:
        pod_name = pod.get("metadata", {}).get("name")
        pod_ip = pod.get("status", {}).get("podIP")
        if not pod_ip:
            continue
        endpoints.append(
            {
                "addresses": [pod_ip],
                "conditions": {
                    "ready": pod_is_ready(pod),
                    "serving": pod_is_ready(pod),
                    "terminating": bool(pod.get("metadata", {}).get("deletionTimestamp")),
                },
                "targetRef": {
                    "kind": "Pod",
                    "name": pod_name,
                    "namespace": namespace,
                    "uid": pod.get("metadata", {}).get("uid"),
                },
            }
        )
    return new_object(
        C.DISCOVERY_API_VERSION,
        C.KIND_ENDPOINTSLICE,
        name,
        namespace,
        spec=None,
        status=None,
        labels={"kubernetes.io/service-name": service_name, **(labels or {})},
        owner_references=owner_references,
    ) | {
        "addressType": "IPv4",
        "endpoints": endpoints,
        "ports": deep_copy(ports or []),
    }


def new_network_policy(
    name: str,
    namespace: str,
    pod_selector: dict,
    *,
    policy_types: list[str],
    ingress: list[dict] | None = None,
    egress: list[dict] | None = None,
    labels: dict | None = None,
    owner_references: list[dict] | None = None,
) -> dict:
    return new_object(
        C.NETWORKING_API_VERSION,
        C.KIND_NETWORKPOLICY,
        name,
        namespace,
        spec={
            "podSelector": deep_copy(pod_selector),
            "policyTypes": list(policy_types),
            "ingress": deep_copy(ingress or []),
            "egress": deep_copy(egress or []),
        },
        labels=labels,
        owner_references=owner_references,
    )


def new_pvc(
    name: str,
    namespace: str,
    size: str,
    *,
    storage_class: str | None = None,
    access_modes: list[str] | None = None,
    labels: dict | None = None,
    owner_references: list[dict] | None = None,
    retain: bool = False,
) -> dict:
    return new_object(
        C.CORE_API_VERSION,
        C.KIND_PVC,
        name,
        namespace,
        spec={
            "accessModes": list(access_modes or ["ReadWriteOnce"]),
            "resources": {"requests": {"storage": size}},
            **({"storageClassName": storage_class} if storage_class else {}),
        },
        status={"phase": "Bound"},
        labels=labels,
        owner_references=[] if retain else owner_references,
    )


def new_secret(name: str, namespace: str, data: dict[str, str], *, labels: dict | None = None, owner_references: list[dict] | None = None) -> dict:
    return new_object(
        C.CORE_API_VERSION,
        C.KIND_SECRET,
        name,
        namespace,
        spec=None,
        labels=labels,
        owner_references=owner_references,
    ) | {"type": "Opaque", "data": dict(data)}


def pod_phase(pod: dict) -> str:
    return pod.get("status", {}).get("phase", "Pending")


def pod_is_ready(pod: dict) -> bool:
    if pod_phase(pod) != "Running":
        return False
    for condition in pod.get("status", {}).get("conditions") or []:
        if condition.get("type") == "Ready":
            return condition.get("status") == "True"
    return False


def pod_is_terminating(pod: dict) -> bool:
    return bool(pod.get("metadata", {}).get("deletionTimestamp"))


def pod_containers(pod: dict) -> list[dict]:
    return pod.get("spec", {}).get("containers") or []


def pod_requested_resources(pod: dict) -> dict[str, float]:
    from ..util import resource_quantity

    totals = {"cpu": 0.0, "memory": 0.0, "nvidia.com/gpu": 0.0}
    for item in pod_containers(pod):
        requests = item.get("resources", {}).get("requests", {})
        for key in list(totals):
            totals[key] += resource_quantity(requests, key)
        for key, value in requests.items():
            if key not in totals:
                totals[key] = totals.get(key, 0.0) + resource_quantity({key: value}, key)
    return totals


def pod_ray_container(pod: dict) -> dict | None:
    return get_ray_container(pod.get("spec", {}))


def job_succeeded(job: dict) -> bool:
    return int(job.get("status", {}).get("succeeded", 0)) > 0


def job_failed(job: dict) -> bool:
    return int(job.get("status", {}).get("failed", 0)) > 0


def job_finished(job: dict) -> bool:
    return job_succeeded(job) or job_failed(job)


def service_selects(service: dict, pod: dict) -> bool:
    selector = service.get("spec", {}).get("selector") or {}
    pod_labels = pod.get("metadata", {}).get("labels") or {}
    if not selector:
        return False
    return all(pod_labels.get(key) == value for key, value in selector.items())


def service_pods(service: dict, pods: list[dict]) -> list[dict]:
    return [pod for pod in pods if service_selects(service, pod) and not pod_is_terminating(pod)]


def endpoint_count(endpoint_slice: dict) -> int:
    return sum(
        1
        for endpoint in endpoint_slice.get("endpoints") or []
        if (endpoint.get("conditions") or {}).get("ready", True)
    )


def matches_label_selector(obj: dict, selector: dict | None) -> bool:
    return labels_match(selector, obj.get("metadata", {}).get("labels") or {})
