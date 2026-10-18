"""KubeRay In-Place Pod Resizing (IPPR) model.

Upstream KubeRay enables IPPR through the ``ray.io/ippr`` annotation on a
RayCluster.  The annotation is a JSON document keyed by worker group::

    ray.io/ippr: '{"groups":{"small":{"max-cpu":"4","max-memory":"4Gi",
                    "resize-timeout":60}}}'

The real autoscaler (v2 only) bins pending demand against each Pod's *maximum*
capacity; when a running Pod can absorb the demand it issues a Kubernetes
``resize`` request and records progress in the ``ray.io/ippr-status`` Pod
annotation.  This module owns the parsing/validation and the Pod mutation
helpers so that both the controller and the tests use one definition of the
contract.

The status annotation written here is a teaching-compatible subset of the
upstream shape.  Read it through :func:`status_of` instead of depending on
individual keys.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from .. import constants as C
from ..clock import iso, parse_iso
from ..util import format_cpu, format_memory, get_ray_container, parse_cpu, parse_memory

IPPR_STATE_RESIZING = "resizing"
IPPR_STATE_SUCCEEDED = "succeeded"
IPPR_STATE_FAILED = "failed"
IPPR_STATE_ROLLED_BACK = "rolled-back"

RESIZABLE_RESOURCES = ("cpu", "memory")


@dataclass(frozen=True)
class IPPRGroupConfig:
    """Validated per-group IPPR limits. CPU is millicores, memory is bytes."""

    group: str
    max_cpu: int
    max_memory: int
    timeout_seconds: int


@dataclass(frozen=True)
class PodResources:
    cpu: int
    memory: int
    gpu: float
    resources: dict[str, float]


def _group(spec: dict, name: str) -> dict | None:
    for group in spec.get("workerGroupSpecs") or []:
        if group.get("groupName") == name:
            return group
    return None


def _ray_container_from_group(group: dict) -> dict | None:
    template = group.get("template") or {}
    return get_ray_container(template.get("spec") or {})


def _resize_policy_errors(group_name: str, container: dict, errors: list[str]) -> None:
    """KubeRay only supports in-place changes; a restarting resize is rejected."""
    policies = container.get("resizePolicy") or []
    if not isinstance(policies, list):
        errors.append(f"worker group {group_name}: resizePolicy must be a list")
        return
    for item in policies:
        if not isinstance(item, dict):
            errors.append(f"worker group {group_name}: resizePolicy entries must be objects")
            continue
        resource = item.get("resourceName")
        policy = item.get("restartPolicy")
        if resource in RESIZABLE_RESOURCES and policy not in (None, "NotRequired"):
            errors.append(
                f"worker group {group_name}: resizePolicy for {resource} must be "
                f"NotRequired, got {policy!r}"
            )


def parse_ippr(cluster: dict) -> tuple[dict[str, IPPRGroupConfig], list[str]]:
    """Parse and validate ``ray.io/ippr``.

    Returns ``(configs, errors)``.  An absent annotation is valid and yields an
    empty mapping.  The checks mirror the upstream autoscaler preconditions:
    autoscaler v2, no ``num-cpus``/``memory`` in ``rayStartParams``, explicit
    CPU+memory requests, and no restarting resize policy.
    """
    errors: list[str] = []
    annotations = (cluster.get("metadata") or {}).get("annotations") or {}
    raw = annotations.get(C.ANNOTATION_IPPR)
    if raw in (None, ""):
        return {}, errors
    spec = cluster.get("spec") or {}
    try:
        document = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError) as exc:
        return {}, [f"annotation {C.ANNOTATION_IPPR} must be valid JSON: {exc}"]
    if not isinstance(document, dict):
        return {}, [f"annotation {C.ANNOTATION_IPPR} must be a JSON object"]
    groups = document.get("groups")
    if not isinstance(groups, dict) or not groups:
        return {}, [f"annotation {C.ANNOTATION_IPPR} must contain a non-empty groups object"]

    if not spec.get("enableInTreeAutoscaling"):
        errors.append("IPPR requires spec.enableInTreeAutoscaling=true")
    options = spec.get("autoscalerOptions") or {}
    if options.get("version", "v2") != "v2":
        errors.append("IPPR requires autoscalerOptions.version=v2")

    configs: dict[str, IPPRGroupConfig] = {}
    for group_name, config in groups.items():
        if not isinstance(config, dict):
            errors.append(f"IPPR group {group_name!r} must be an object")
            continue
        group = _group(spec, group_name)
        if group is None:
            errors.append(f"IPPR group {group_name!r} does not match any workerGroupSpecs[].groupName")
            continue
        allowed = {"max-cpu", "max-memory", "resize-timeout"}
        unknown = sorted(set(config) - allowed)
        if unknown:
            errors.append(f"IPPR group {group_name!r} has unknown fields: {', '.join(unknown)}")
            continue
        missing = [key for key in sorted(allowed) if key not in config]
        if missing:
            errors.append(f"IPPR group {group_name!r} is missing {', '.join(missing)}")
            continue
        # JSON booleans are Python ints; ``true`` must not silently become 1.
        boolean_keys = [key for key in ("max-cpu", "max-memory", "resize-timeout") if isinstance(config[key], bool)]
        if boolean_keys:
            errors.append(f"IPPR group {group_name!r} has boolean values for {', '.join(boolean_keys)}")
            continue
        timeout_raw = config["resize-timeout"]
        if isinstance(timeout_raw, float) and not timeout_raw.is_integer():
            errors.append(f"IPPR group {group_name!r} resize-timeout must be an integer number of seconds")
            continue
        try:
            max_cpu = parse_cpu(config["max-cpu"])
            max_memory = parse_memory(config["max-memory"])
            timeout = int(timeout_raw)
        except (TypeError, ValueError) as exc:
            errors.append(f"IPPR group {group_name!r} has an invalid value: {exc}")
            continue
        if max_cpu <= 0:
            errors.append(f"IPPR group {group_name!r} max-cpu must be positive")
        if max_memory <= 0:
            errors.append(f"IPPR group {group_name!r} max-memory must be positive")
        if timeout <= 0:
            errors.append(f"IPPR group {group_name!r} resize-timeout must be a positive integer")

        params = group.get("rayStartParams") or {}
        for forbidden in ("num-cpus", "memory"):
            if forbidden in params:
                errors.append(
                    f"IPPR group {group_name!r} must not set rayStartParams.{forbidden}; "
                    "Ray must derive resources from the Pod requests"
                )

        container = _ray_container_from_group(group)
        if container is None:
            errors.append(f"IPPR group {group_name!r} has no Ray container")
            continue
        _resize_policy_errors(group_name, container, errors)
        requests = (container.get("resources") or {}).get("requests") or {}
        if "cpu" not in requests or "memory" not in requests:
            errors.append(f"IPPR group {group_name!r} requires both CPU and memory requests")
            continue
        current_cpu = parse_cpu(requests.get("cpu"))
        current_memory = parse_memory(requests.get("memory"))
        if max_cpu < current_cpu:
            errors.append(
                f"IPPR group {group_name!r} max-cpu ({config['max-cpu']}) is below the "
                f"current request ({format_cpu(current_cpu)})"
            )
        if max_memory < current_memory:
            errors.append(
                f"IPPR group {group_name!r} max-memory ({config['max-memory']}) is below "
                f"the current request ({format_memory(current_memory)})"
            )
        configs[group_name] = IPPRGroupConfig(group_name, max_cpu, max_memory, timeout)
    return configs, errors


def status_of(pod: dict) -> dict:
    """Return the parsed ``ray.io/ippr-status`` annotation (or ``{}``)."""
    raw = ((pod.get("metadata") or {}).get("annotations") or {}).get(C.ANNOTATION_IPPR_STATUS)
    if not raw:
        return {}
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def pod_resources(pod: dict) -> PodResources:
    """Current requested resources of the Ray container."""
    container = get_ray_container(pod.get("spec") or {}) or {}
    requests = (container.get("resources") or {}).get("requests") or {}
    return PodResources(
        cpu=parse_cpu(requests.get("cpu")),
        memory=parse_memory(requests.get("memory")),
        gpu=float(requests.get("nvidia.com/gpu", 0) or 0),
        resources={key: float(_quantity(value, key)) for key, value in requests.items()},
    )


def _quantity(value: Any, resource: str) -> float:
    if resource == "cpu":
        return parse_cpu(value)
    if resource == "memory":
        return parse_memory(value)
    return float(value)


def _set_condition(pod: dict, condition_type: str, state: bool, reason: str, message: str, now: str) -> None:
    status = pod.setdefault("status", {})
    conditions = status.setdefault("conditions", [])
    for item in conditions:
        if item.get("type") == condition_type:
            item.update(
                {
                    "status": "True" if state else "False",
                    "reason": reason,
                    "message": message,
                    "lastTransitionTime": now,
                }
            )
            return
    conditions.append(
        {
            "type": condition_type,
            "status": "True" if state else "False",
            "reason": reason,
            "message": message,
            "lastTransitionTime": now,
        }
    )


def _remove_condition(pod: dict, condition_type: str) -> None:
    status = pod.setdefault("status", {})
    conditions = status.get("conditions")
    if isinstance(conditions, list):
        status["conditions"] = [item for item in conditions if item.get("type") != condition_type]


def apply_resize(pod: dict, config: IPPRGroupConfig, target_cpu: int, target_memory: int, *, clock) -> dict:
    """Patch the Ray container and record a ``resizing`` status annotation.

    The Pod UID does not change: this is the difference between IPPR and a
    rolling recreate.  The simulated kubelet observes the annotation and
    completes (or fails) the resize on a later step.
    """
    pod = _deep_copy(pod)
    container = get_ray_container(pod.get("spec") or {})
    if container is None:
        raise ValueError("pod has no Ray container")
    resources = container.setdefault("resources", {})
    requests = resources.setdefault("requests", {})
    # Do not create an empty ``limits`` map: the resize only needs to raise
    # limits that the user actually set.
    limits = resources.get("limits") or {}
    previous_cpu = parse_cpu(requests.get("cpu"))
    previous_memory = parse_memory(requests.get("memory"))
    previous_cpu_limit = format_cpu(parse_cpu(limits["cpu"])) if "cpu" in limits else None
    previous_memory_limit = format_memory(parse_memory(limits["memory"])) if "memory" in limits else None
    target_cpu = max(previous_cpu, int(target_cpu))
    target_memory = max(previous_memory, int(target_memory))
    requests["cpu"] = format_cpu(target_cpu)
    requests["memory"] = format_memory(target_memory)
    # A request may never exceed a limit.  Only touch limits the user set; an
    # unset limit continues to mean "no limit".
    if "cpu" in limits and parse_cpu(limits["cpu"]) < target_cpu:
        limits["cpu"] = format_cpu(target_cpu)
    if "memory" in limits and parse_memory(limits["memory"]) < target_memory:
        limits["memory"] = format_memory(target_memory)

    now = iso(clock.now())
    previous = status_of(pod)
    attempts = int(previous.get("attempts", 0) or 0) + 1
    status = {
        "state": IPPR_STATE_RESIZING,
        "group": config.group,
        "resizing_at": now,
        "timeout_seconds": config.timeout_seconds,
        "previous_cpu": format_cpu(previous_cpu),
        "previous_memory": format_memory(previous_memory),
        "previous_cpu_limit": previous_cpu_limit,
        "previous_memory_limit": previous_memory_limit,
        "target_cpu": format_cpu(target_cpu),
        "target_memory": format_memory(target_memory),
        "attempts": attempts,
    }
    pod.setdefault("metadata", {}).setdefault("annotations", {})[C.ANNOTATION_IPPR_STATUS] = json.dumps(
        status, sort_keys=True
    )
    _set_condition(
        pod,
        "PodResizeInProgress",
        True,
        "InPlaceResize",
        f"resizing {config.group} from {format_cpu(previous_cpu)}/{format_memory(previous_memory)} "
        f"to {format_cpu(target_cpu)}/{format_memory(target_memory)}",
        now,
    )
    return pod


def complete_resize(pod: dict, *, clock) -> tuple[dict, bool]:
    """Kubelet-side success transition. Returns ``(pod, changed)``."""
    status = status_of(pod)
    if status.get("state") != IPPR_STATE_RESIZING:
        return pod, False
    now = iso(clock.now())
    pod = _deep_copy(pod)
    status["state"] = IPPR_STATE_SUCCEEDED
    status["completed_at"] = now
    pod["metadata"]["annotations"][C.ANNOTATION_IPPR_STATUS] = json.dumps(status, sort_keys=True)
    # KEP-1287: the condition is removed when the resize succeeds; the
    # annotation and containerStatuses carry the historical evidence.
    _remove_condition(pod, "PodResizeInProgress")
    container = get_ray_container(pod.get("spec") or {}) or {}
    container_resources = container.get("resources") or {}
    effective: dict[str, dict] = {}
    if container_resources.get("requests"):
        effective["requests"] = _deep_copy(container_resources["requests"])
    if container_resources.get("limits"):
        effective["limits"] = _deep_copy(container_resources["limits"])
    containers = (pod.get("status") or {}).get("containerStatuses") or []
    for item in containers:
        if item.get("name") == "ray":
            # Keep GPU and every other resource; IPPR only changed CPU/memory.
            item["resources"] = effective
    return pod, True


def fail_resize(pod: dict, *, clock, reason: str = "ResizeTimeout") -> tuple[dict, bool]:
    """Kubelet-side failure transition. The controller decides whether to roll back."""
    status = status_of(pod)
    if status.get("state") != IPPR_STATE_RESIZING:
        return pod, False
    now = iso(clock.now())
    pod = _deep_copy(pod)
    status["state"] = IPPR_STATE_FAILED
    status["last_failed_at"] = now
    status["message"] = reason
    pod["metadata"]["annotations"][C.ANNOTATION_IPPR_STATUS] = json.dumps(status, sort_keys=True)
    _set_condition(pod, "PodResizeInProgress", False, reason, "in-place resize did not complete", now)
    return pod, True


def rollback(pod: dict, *, clock, retry_seconds: int | None = None) -> tuple[dict, bool]:
    """Restore the previous request and mark the attempt as rolled back."""
    status = status_of(pod)
    if status.get("state") != IPPR_STATE_FAILED:
        return pod, False
    pod = _deep_copy(pod)
    container = get_ray_container(pod.get("spec") or {})
    if container is None:
        return pod, False
    resources = container.setdefault("resources", {})
    requests = resources.setdefault("requests", {})
    limits = resources.get("limits") or {}
    previous_cpu = parse_cpu(status.get("previous_cpu"))
    previous_memory = parse_memory(status.get("previous_memory"))
    requests["cpu"] = format_cpu(previous_cpu)
    requests["memory"] = format_memory(previous_memory)
    if "cpu" in limits:
        limits["cpu"] = status.get("previous_cpu_limit") or format_cpu(previous_cpu)
    if "memory" in limits:
        limits["memory"] = status.get("previous_memory_limit") or format_memory(previous_memory)
    now = clock.now()
    status["state"] = IPPR_STATE_ROLLED_BACK
    status["rolled_back_at"] = iso(now)
    timeout = int(status.get("timeout_seconds", retry_seconds or C.DEFAULT_IPPR_RESIZE_SECONDS))
    from datetime import timedelta

    status["retry_after"] = iso(now + timedelta(seconds=retry_seconds if retry_seconds is not None else timeout))
    pod["metadata"]["annotations"][C.ANNOTATION_IPPR_STATUS] = json.dumps(status, sort_keys=True)
    _set_condition(pod, "PodResizeInProgress", False, "ResizeRolledBack", "previous resources restored", iso(now))
    return pod, True


def retry_allowed(pod: dict, *, clock) -> bool:
    """A rolled-back Pod waits for its retry window before another resize."""
    status = status_of(pod)
    if status.get("state") != IPPR_STATE_ROLLED_BACK:
        return True
    retry_after = status.get("retry_after")
    if not retry_after:
        return True
    try:
        return clock.now() >= parse_iso(retry_after)
    except (TypeError, ValueError):
        return True


def _deep_copy(value: Any) -> Any:
    from ..util import deep_copy

    return deep_copy(value)
