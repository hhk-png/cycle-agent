"""Spec validation.

The checks are intentionally close to KubeRay's Go validation so that errors
shown by MiniKubeRay teach the same lessons as errors shown by the operator.
"""

from __future__ import annotations

import json
import re
from typing import Any

from .. import constants as C
from ..errors import Invalid
from ..util import (
    env_value,
    get_ray_container,
    is_dns1035_label,
    is_dns1123_label,
    parse_duration_seconds,
    ray_supports_autoscaler_v2,
    resource_quantity,
    semver_tuple,
)
from .raycronjob import validate_cron

MAX_NAME_LENGTH = {
    C.KIND_RAYCLUSTER: 53,
    C.KIND_RAYJOB: 47,
    C.KIND_RAYSERVICE: 47,
    C.KIND_RAYCRONJOB: 36,
}


def _err(errors: list[str], message: str) -> None:
    errors.append(message)


def validate_metadata(obj: dict, errors: list[str]) -> None:
    metadata = obj.get("metadata") or {}
    obj_name = metadata.get("name", "")
    kind = obj.get("kind", "")
    max_len = MAX_NAME_LENGTH.get(kind, 63)
    if not obj_name:
        _err(errors, "metadata.name is required")
    elif len(obj_name) > max_len:
        _err(errors, f"{kind} name should be no more than {max_len} characters, got {len(obj_name)}")
    elif not is_dns1035_label(obj_name):
        _err(errors, f"{kind} name must be a valid DNS-1035 label (start with a lowercase letter): {obj_name!r}")
    namespace = metadata.get("namespace", "default")
    if namespace and not is_dns1123_label(namespace):
        _err(errors, f"metadata.namespace is not a valid DNS-1123 label: {namespace!r}")
    for key, value in (metadata.get("labels") or {}).items():
        if not key or len(key) > 253:
            _err(errors, f"invalid label key: {key!r}")
        if len(str(value)) > 63:
            _err(errors, f"label {key!r} value exceeds 63 characters")


def _validate_containers(group_name: str, template: dict, errors: list[str], *, require_ray: bool = True) -> None:
    if not isinstance(template, dict):
        _err(errors, f"{group_name}: pod template must be an object")
        return
    spec = template.get("spec") or {}
    if not isinstance(spec, dict):
        _err(errors, f"{group_name}: pod template spec must be an object")
        return
    containers = spec.get("containers") or []
    if not isinstance(containers, list):
        _err(errors, f"{group_name}: spec.containers must be a list")
        return
    if not containers:
        _err(errors, f"{group_name}: pod template must have at least one container")
        return
    names = [item.get("name") for item in containers]
    if require_ray and "ray" not in names:
        _err(errors, f"{group_name}: the Ray container must be named 'ray'")
    if require_ray and names and names[0] != "ray":
        _err(errors, f"{group_name}: the Ray container must be the first container (found {names[0]!r})")
    for item in containers:
        resources = item.get("resources") or {}
        requests = resources.get("requests") or {}
        limits = resources.get("limits") or {}
        for resource, request in requests.items():
            if resource in limits:
                try:
                    if resource_quantity(requests, resource) > resource_quantity(limits, resource):
                        _err(errors, f"{group_name}/{item.get('name')}: request for {resource} exceeds limit")
                except ValueError as exc:
                    _err(errors, f"{group_name}/{item.get('name')}: invalid quantity: {exc}")
        gpu = limits.get("nvidia.com/gpu")
        if gpu is not None:
            try:
                gpu_value = float(gpu)
            except (TypeError, ValueError):
                _err(errors, f"{group_name}/{item.get('name')}: nvidia.com/gpu must be an integer")
                continue
            if gpu_value < 0 or not gpu_value.is_integer():
                _err(errors, f"{group_name}/{item.get('name')}: nvidia.com/gpu must be a non-negative integer")


def validate_raycluster_spec(spec: dict, errors: list[str]) -> None:
    if not spec:
        _err(errors, "spec is required")
        return
    ray_version = str(spec.get("rayVersion") or "")
    if semver_tuple(ray_version) is None:
        _err(errors, f"spec.rayVersion must be a semantic version such as 2.59.0, got {ray_version!r}")

    upgrade = spec.get("upgradeStrategy") or {}
    if upgrade.get("type") not in (None, "", *C.RAY_CLUSTER_UPGRADE_TYPES):
        _err(errors, f"spec.upgradeStrategy.type must be one of {C.RAY_CLUSTER_UPGRADE_TYPES}")

    managed_by = spec.get("managedBy")
    if managed_by not in (None, C.MANAGED_BY_KUBERAY, C.MANAGED_BY_MULTIKUEUE):
        _err(errors, "spec.managedBy must be 'ray.io/kuberay-operator' or 'kueue.x-k8s.io/multikueue'")

    head = spec.get("headGroupSpec")
    if head is None:
        _err(errors, "spec.headGroupSpec is required")
        return
    if not isinstance(head, dict):
        _err(errors, "spec.headGroupSpec must be an object")
        return
    if not head:
        _err(errors, "spec.headGroupSpec is required")
        return
    _validate_containers("headGroupSpec", head.get("template") or {}, errors)
    _validate_group_resources("headGroupSpec", head, errors)
    _validate_group_labels("headGroupSpec", head, errors)

    autoscaling = bool(spec.get("enableInTreeAutoscaling"))
    groups = spec.get("workerGroupSpecs")
    if groups is None:
        groups = []
    if not isinstance(groups, list):
        _err(errors, "spec.workerGroupSpecs must be a list")
        return
    seen = set()
    for index, group in enumerate(groups):
        if not isinstance(group, dict):
            _err(errors, f"workerGroupSpecs[{index}] must be an object")
            continue
        group_name = group.get("groupName") or f"worker-group-{index}"
        if group_name in seen:
            _err(errors, f"duplicate worker group name: {group_name!r}")
        seen.add(group_name)
        if not is_dns1123_label(group_name):
            _err(errors, f"worker group name {group_name!r} must be a DNS-1123 label")
        _validate_containers(f"workerGroupSpecs[{group_name}]", group.get("template") or {}, errors)
        _validate_group_resources(group_name, group, errors)
        _validate_group_labels(group_name, group, errors)

        replicas = int(group.get("replicas", 0))
        min_replicas = int(group.get("minReplicas", 0))
        max_replicas = int(group.get("maxReplicas", C.MAX_INT32))
        if replicas < 0:
            _err(errors, f"worker group {group_name} has negative replicas {replicas}")
        if min_replicas < 0:
            _err(errors, f"worker group {group_name} has negative minReplicas {min_replicas}")
        if max_replicas < 0:
            _err(errors, f"worker group {group_name} has negative maxReplicas {max_replicas}")
        if min_replicas > max_replicas:
            _err(errors, f"worker group {group_name} has minReplicas {min_replicas} greater than maxReplicas {max_replicas}")
        if autoscaling and (group.get("minReplicas") is None or group.get("maxReplicas") is None):
            # The CRD supplies defaults, so this mainly protects direct API
            # callers.  Even without autoscaling, `replicas` is clamped to
            # [minReplicas, maxReplicas] by group_desired_pods().
            _err(errors, f"worker group {group_name} must set minReplicas and maxReplicas when autoscaling is enabled")
        num_hosts = int(group.get("numOfHosts", 1) or 1)
        if num_hosts < 1:
            _err(errors, f"worker group {group_name} numOfHosts must be >= 1")
        idle = group.get("idleTimeoutSeconds")
        if idle is not None and int(idle) < 0:
            _err(errors, f"worker group {group_name} idleTimeoutSeconds must be non-negative")
        if group.get("suspend") and autoscaling:
            _err(errors, f"worker group {group_name} cannot be suspended while autoscaling is enabled")
        if group.get("suspend") and not autoscaling:
            _err(errors, f"worker group {group_name} can be suspended only through RayJob deletion policy")
        if group.get("labelRefs"):
            if not (semver_tuple(ray_version) and semver_tuple(ray_version) >= (2, 45, 0)):
                _err(errors, f"worker group {group_name} labelRefs requires Ray >= 2.45.0")

    options = spec.get("autoscalerOptions") or {}
    if not isinstance(options, dict):
        _err(errors, "spec.autoscalerOptions must be an object")
        options = {}
    if autoscaling:
        if options.get("upscalingMode") not in (None, *C.UPSCALING_MODES):
            _err(errors, f"autoscalerOptions.upscalingMode must be one of {C.UPSCALING_MODES}")
        if options.get("version") not in (None, *C.AUTOSCALER_VERSIONS):
            _err(errors, f"autoscalerOptions.version must be one of {C.AUTOSCALER_VERSIONS}")
        if options.get("idleTimeoutSeconds") is not None and int(options["idleTimeoutSeconds"]) < 0:
            _err(errors, "autoscalerOptions.idleTimeoutSeconds must be non-negative")
        if options.get("version") and env_value(get_ray_container(head.get("template", {}).get("spec", {})) or {}, "RAY_enable_autoscaler_v2") is not None:
            _err(errors, "autoscalerOptions.version and RAY_enable_autoscaler_v2 cannot both be set")
        if not ray_supports_autoscaler_v2(ray_version) and options.get("version") == "v2":
            _err(errors, "autoscaler v2 requires Ray >= 2.47.0")
    else:
        head_container = get_ray_container((head.get("template") or {}).get("spec") or {}) or {}
        if env_value(head_container, "RAY_enable_autoscaler_v2") in ("1", "true"):
            _err(errors, "RAY_enable_autoscaler_v2 cannot be true when enableInTreeAutoscaling is false")

    ft = spec.get("gcsFaultToleranceOptions")
    if ft and not isinstance(ft, dict):
        _err(errors, "spec.gcsFaultToleranceOptions must be an object")
        ft = None
    if ft:
        backend = ft.get("backend", "redis")
        if backend not in C.GCS_FT_BACKENDS:
            _err(errors, f"gcsFaultToleranceOptions.backend must be one of {C.GCS_FT_BACKENDS}")
        if backend == "redis":
            if not ft.get("redisAddress"):
                _err(errors, "gcsFaultToleranceOptions.redisAddress is required for the redis backend")
            active_passive = ft.get("activePassiveHeadOptions") or {}
            if active_passive.get("enabled"):
                lease = int(active_passive.get("leaseDurationSeconds", 15))
                renew = int(active_passive.get("renewDeadlineSeconds", 10))
                retry = int(active_passive.get("retryPeriodSeconds", 2))
                if not (lease > renew > retry):
                    _err(errors, "active-passive head requires leaseDurationSeconds > renewDeadlineSeconds > retryPeriodSeconds")
        else:
            if ft.get("redisAddress"):
                _err(errors, "redisAddress must be empty for the rocksdb backend")

    auth = spec.get("authOptions") or {}
    if not isinstance(auth, dict):
        _err(errors, "spec.authOptions must be an object")
        auth = {}
    if auth.get("mode", "token") not in C.AUTH_MODES:
        _err(errors, f"authOptions.mode must be one of {C.AUTH_MODES}")

    network = spec.get("networkPolicy") or {}
    if not isinstance(network, dict):
        _err(errors, "spec.networkPolicy must be an object")
        network = {}
    if network and network.get("mode", "DenyAll") not in C.NETWORK_POLICY_MODES:
        _err(errors, f"networkPolicy.mode must be one of {C.NETWORK_POLICY_MODES}")

    history = spec.get("historyServerOptions") or {}
    if not isinstance(history, dict):
        _err(errors, "spec.historyServerOptions must be an object")
        history = {}
    if history and not (history.get("collectorOptions") or {}).get("image"):
        _err(errors, "historyServerOptions.collectorOptions.image is required")


def _validate_group_resources(group_name: str, group: dict, errors: list[str]) -> None:
    params = group.get("rayStartParams") or {}
    resources = group.get("resources") or {}
    has_params = any(params.get(key) for key in ("num-cpus", "num-gpus", "memory", "resources"))
    if has_params and resources:
        _err(errors, f"{group_name}: do not set resource fields in both rayStartParams and top-level resources")


def _validate_group_labels(group_name: str, group: dict, errors: list[str]) -> None:
    if "labels" in (group.get("rayStartParams") or {}):
        _err(errors, f"{group_name}: rayStartParams['labels'] is not supported; use top-level labels")
    for key, value in (group.get("labels") or {}).items():
        if not key:
            _err(errors, f"{group_name}: empty label key")
        if len(str(value)) > 63:
            _err(errors, f"{group_name}: label {key!r} value exceeds 63 characters")


def validate_raycluster(obj: dict) -> list[str]:
    errors: list[str] = []
    validate_metadata(obj, errors)
    validate_raycluster_spec(obj.get("spec") or {}, errors)
    # IPPR lives on the CR annotation and constrains the worker groups, so it
    # cannot be checked by the spec-only validator.
    from .ippr import parse_ippr

    _, ippr_errors = parse_ippr(obj)
    errors.extend(ippr_errors)
    status = obj.get("status") or {}
    from .meta import is_condition_true

    if is_condition_true(status, C.COND_SUSPENDING) and is_condition_true(status, C.COND_SUSPENDED):
        _err(errors, "RayClusterSuspending and RayClusterSuspended cannot both be true")
    return errors


def _validate_deletion_configuration(spec: dict, errors: list[str]) -> None:
    shutdown = bool(spec.get("shutdownAfterJobFinishes"))
    ttl = int(spec.get("ttlSecondsAfterFinished", 0) or 0)
    if not shutdown and ttl > 0:
        _err(errors, "ttlSecondsAfterFinished requires shutdownAfterJobFinishes=true")
    strategy = spec.get("deletionStrategy")
    if not strategy:
        return
    legacy = strategy.get("onSuccess") is not None or strategy.get("onFailure") is not None
    rules = strategy.get("deletionRules") or []
    if shutdown:
        _err(errors, "shutdownAfterJobFinishes and deletionStrategy are mutually exclusive")
    if ttl > 0:
        _err(errors, "ttlSecondsAfterFinished and deletionStrategy are mutually exclusive")
    if rules and legacy:
        _err(errors, "legacy onSuccess/onFailure and deletionRules are mutually exclusive")
    if legacy and not (strategy.get("onSuccess") is not None and strategy.get("onFailure") is not None):
        _err(errors, "deletionStrategy requires both onSuccess and onFailure")
    if not legacy and not rules:
        _err(errors, "deletionStrategy requires deletionRules or both onSuccess and onFailure")
    for index, rule in enumerate(rules):
        policy = rule.get("policy")
        if policy not in C.DELETION_POLICIES:
            _err(errors, f"deletionRules[{index}].policy must be one of {C.DELETION_POLICIES}")
        condition = rule.get("condition") or {}
        if bool(condition.get("jobStatus")) == bool(condition.get("jobDeploymentStatus")):
            _err(errors, f"deletionRules[{index}].condition requires exactly one of jobStatus or jobDeploymentStatus")
        if int(condition.get("ttlSeconds", 0) or 0) < 0:
            _err(errors, f"deletionRules[{index}].condition.ttlSeconds must be non-negative")


def validate_rayjob(obj: dict) -> list[str]:
    errors: list[str] = []
    validate_metadata(obj, errors)
    spec = obj.get("spec") or {}
    mode = spec.get("submissionMode", "K8sJobMode")
    if mode not in C.SUBMISSION_MODES:
        _err(errors, f"submissionMode must be one of {C.SUBMISSION_MODES}")
    selector = spec.get("clusterSelector") or {}
    if spec.get("suspend") and not spec.get("shutdownAfterJobFinishes"):
        _err(errors, "a RayJob with shutdownAfterJobFinishes=false cannot be suspended")
    if selector:
        if spec.get("suspend"):
            _err(errors, "ClusterSelector mode does not support suspend")
        if not selector.get(C.LABEL_CLUSTER):
            _err(errors, "clusterSelector must contain 'ray.io/cluster'")
        if mode == "SidecarMode":
            _err(errors, "ClusterSelector is not supported in SidecarMode")
        if int(spec.get("backoffLimit", 0) or 0) > 0:
            _err(errors, "BackoffLimit is incompatible with ClusterSelector mode")
        if spec.get("shutdownAfterJobFinishes"):
            _err(errors, "shutdownAfterJobFinishes cannot be used with clusterSelector; the shared cluster is not owned by the RayJob")
        strategy = spec.get("deletionStrategy") or {}
        policies = [rule.get("policy") for rule in strategy.get("deletionRules") or []]
        policies.extend([strategy.get("onSuccess"), strategy.get("onFailure")])
        if any(policy in ("DeleteCluster", "DeleteWorkers") for policy in policies):
            _err(
                errors,
                "deletionStrategy cannot delete a shared cluster or its workers in clusterSelector mode",
            )
        if spec.get("rayClusterSpec") is not None:
            _err(errors, "rayClusterSpec and clusterSelector are mutually exclusive")
    elif spec.get("rayClusterSpec") is None:
        _err(errors, "one of rayClusterSpec or clusterSelector must be set")
    if mode == "InteractiveMode" and int(spec.get("backoffLimit", 0) or 0) > 0:
        _err(errors, "BackoffLimit is incompatible with InteractiveMode")
    if mode == "SidecarMode":
        if spec.get("submitterPodTemplate") is not None:
            _err(errors, "SidecarMode does not support submitterPodTemplate")
        if spec.get("submitterConfig") is not None:
            _err(errors, "SidecarMode does not support submitterConfig")
        cluster = spec.get("rayClusterSpec") or {}
        head = cluster.get("headGroupSpec") or {}
        restart = (head.get("template") or {}).get("spec", {}).get("restartPolicy")
        if restart not in (None, "", "Never"):
            _err(errors, "restartPolicy for head Pod must be Never or unset in SidecarMode")
    if spec.get("rayClusterSpec") is not None:
        validate_raycluster_spec(spec["rayClusterSpec"], errors)
        if "upgradeStrategy" in spec["rayClusterSpec"]:
            _err(errors, "upgradeStrategy is not supported inside a RayJob's rayClusterSpec")
        auth = spec["rayClusterSpec"].get("authOptions") or {}
        if auth.get("enableK8sTokenAuth"):
            _err(errors, "Kubernetes token auth is not supported for RayJob")
    if mode != "InteractiveMode" and not spec.get("entrypoint"):
        _err(errors, "entrypoint is required unless submissionMode is InteractiveMode")
    if int(spec.get("ttlSecondsAfterFinished", 0) or 0) < 0:
        _err(errors, "ttlSecondsAfterFinished must be a non-negative integer")
    active = spec.get("activeDeadlineSeconds")
    if active is not None and int(active) <= 0:
        _err(errors, "activeDeadlineSeconds must be a positive integer")
    pre = spec.get("preRunningDeadlineSeconds")
    if pre is not None and int(pre) <= 0:
        _err(errors, "preRunningDeadlineSeconds must be a positive integer")
    if spec.get("backoffLimit") is not None and int(spec["backoffLimit"]) < 0:
        _err(errors, "backoffLimit must be a non-negative integer")
    # The entrypoint can reserve resources independently of the RayCluster
    # template.  Upstream forwards these to the Job Submission API as
    # entrypoint_num_cpus / entrypoint_num_gpus / entrypoint_resources.
    for field in ("entrypointNumCpus", "entrypointNumGpus"):
        value = spec.get(field)
        if value is None:
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            _err(errors, f"{field} must be a number")
        else:
            if number < 0:
                _err(errors, f"{field} must be non-negative")
    entrypoint_resources = spec.get("entrypointResources")
    if entrypoint_resources is not None:
        # Upstream models this as a JSON-formatted string, not a map:
        #   entrypointResources: '{"Custom1": 2}'
        if not isinstance(entrypoint_resources, str):
            _err(errors, "entrypointResources must be a JSON string")
        else:
            try:
                parsed = json.loads(entrypoint_resources)
            except json.JSONDecodeError:
                _err(errors, "entrypointResources must be valid JSON")
            else:
                if not isinstance(parsed, dict):
                    _err(errors, "entrypointResources must be a JSON object")
                else:
                    for resource_name, quantity in parsed.items():
                        if isinstance(quantity, bool) or not isinstance(quantity, (int, float)):
                            _err(errors, f"entrypointResources[{resource_name}] must be a number")
                        elif quantity < 0:
                            _err(errors, f"entrypointResources[{resource_name}] must be non-negative")
    _validate_deletion_configuration(spec, errors)
    return errors


def validate_rayservice(obj: dict) -> list[str]:
    errors: list[str] = []
    validate_metadata(obj, errors)
    spec = obj.get("spec") or {}
    cluster = spec.get("rayClusterConfig") or {}
    if not cluster:
        _err(errors, "spec.rayClusterConfig is required")
    else:
        validate_raycluster_spec(cluster, errors)
        if "upgradeStrategy" in cluster:
            _err(errors, "upgradeStrategy is not supported inside a RayService's rayClusterConfig")
        auth = cluster.get("authOptions") or {}
        if auth.get("enableK8sTokenAuth"):
            _err(errors, "Kubernetes token auth is not supported for RayService")
        head_service = (cluster.get("headGroupSpec") or {}).get("headService") or {}
        if (head_service.get("metadata") or {}).get("name"):
            _err(errors, "spec.rayClusterConfig.headGroupSpec.headService.metadata.name must not be set")
    strategy = spec.get("upgradeStrategy") or {}
    strategy_type = strategy.get("type", "NewCluster")
    if strategy_type not in C.RAY_SERVICE_UPGRADE_TYPES:
        _err(errors, f"upgradeStrategy.type must be one of {C.RAY_SERVICE_UPGRADE_TYPES}")
    delay = spec.get("rayClusterDeletionDelaySeconds")
    if delay is not None and int(delay) < 0:
        _err(errors, "rayClusterDeletionDelaySeconds must be non-negative")
    if strategy_type == "NewClusterWithIncrementalUpgrade":
        if not cluster.get("enableInTreeAutoscaling"):
            _err(errors, "NewClusterWithIncrementalUpgrade requires Ray autoscaling")
        options = strategy.get("clusterUpgradeOptions") or {}
        max_surge = int(options.get("maxSurgePercent", 100))
        step = options.get("stepSizePercent")
        interval = options.get("intervalSeconds")
        if max_surge < 0 or max_surge > 100:
            _err(errors, "maxSurgePercent must be between 0 and 100")
        if step is None or int(step) < 0 or int(step) > 100:
            _err(errors, "stepSizePercent must be between 0 and 100")
        elif int(step) > max_surge:
            _err(errors, "stepSizePercent must be less than or equal to maxSurgePercent")
        if interval is None or int(interval) <= 0:
            _err(errors, "intervalSeconds must be greater than 0")
        selectors = [
            bool(options.get("gatewayClassName")),
            bool(options.get("httpRouteName")),
        ]
        if sum(selectors) != 1:
            _err(
                errors,
                "exactly one of gatewayClassName or httpRouteName must be specified",
            )
        if options.get("gatewayRef"):
            _err(errors, "gatewayRef is not a KubeRay v1.7 field; use gatewayClassName or httpRouteName")
    return errors


def validate_raycronjob(obj: dict) -> list[str]:
    errors: list[str] = []
    validate_metadata(obj, errors)
    spec = obj.get("spec") or {}
    schedule = spec.get("schedule", "")
    if not schedule:
        _err(errors, "spec.schedule is required")
    elif "TZ" in schedule or "CRON_TZ" in schedule:
        _err(errors, "use spec.timeZone instead of TZ/CRON_TZ in the schedule")
    else:
        try:
            validate_cron(schedule)
        except ValueError as exc:
            _err(errors, f"invalid cron schedule: {exc}")
    timezone_name = spec.get("timeZone")
    if timezone_name is not None:
        if timezone_name == "":
            _err(errors, "timeZone must not be empty; omit it to use the operator's local time zone")
        else:
            from .raycronjob import load_timezone

            try:
                load_timezone(timezone_name)
            except ValueError:
                _err(errors, f"invalid time zone: {timezone_name!r}")
    template = spec.get("jobTemplate")
    if not template:
        _err(errors, "spec.jobTemplate is required")
    else:
        wrapper = {"apiVersion": C.RAY_API_VERSION, "kind": C.KIND_RAYJOB, "metadata": obj.get("metadata", {}), "spec": template}
        errors.extend(f"jobTemplate: {message}" for message in validate_rayjob(wrapper))
    return errors


def validate_object(obj: dict) -> list[str]:
    kind = obj.get("kind")
    try:
        if kind == C.KIND_RAYCLUSTER:
            return validate_raycluster(obj)
        if kind == C.KIND_RAYJOB:
            return validate_rayjob(obj)
        if kind == C.KIND_RAYSERVICE:
            return validate_rayservice(obj)
        if kind == C.KIND_RAYCRONJOB:
            return validate_raycronjob(obj)
        return []
    except (TypeError, ValueError, KeyError, AttributeError) as exc:
        # Malformed YAML often violates type assumptions before a field-level
        # validator can produce a nicer message.  Surface it as validation
        # failure instead of crashing the CLI/controller.
        return [f"invalid spec: {exc}"]


def assert_valid(obj: dict) -> None:
    errors = validate_object(obj)
    if errors:
        raise Invalid("; ".join(errors))


def validate_annotation_duration(value: Any, annotation: str) -> None:
    try:
        parse_duration_seconds(value)
    except ValueError as exc:
        raise Invalid(f"annotation {annotation} is invalid: {exc}") from exc
