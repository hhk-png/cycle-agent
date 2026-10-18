"""Defaulting, mirroring the mutating behaviour of KubeRay's reconcilers.

The real KubeRay CRDs contain OpenAPI defaults and the Go controllers fill the
rest at reconcile time.  MiniKubeRay keeps those rules in one place so the
API server, CLI and tests all observe the same object.
"""

from __future__ import annotations

from .. import constants as C
from ..util import deep_copy, ray_supports_autoscaler_v2
from .core import container


def _as_int(value, default: int) -> int:
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _ensure_spec(obj: dict) -> dict:
    spec = obj.get("spec")
    if not isinstance(spec, dict):
        spec = {}
        obj["spec"] = spec
    return spec


def _default_template(template: dict, image: str) -> dict:
    template = template if template is not None else {}
    template.setdefault("metadata", {})
    spec = template.setdefault("spec", {})
    if not isinstance(spec, dict):
        return template
    containers = spec.setdefault("containers", [])
    if not isinstance(containers, list):
        return template
    if not containers:
        containers.append(container("ray", image))
    spec.setdefault("restartPolicy", "Never")
    spec.setdefault("terminationGracePeriodSeconds", 30)
    return template


def _default_ray_container(template: dict, image: str) -> dict:
    template = _default_template(template, image)
    containers = template["spec"]["containers"]
    ray = None
    for item in containers:
        if not isinstance(item, dict):
            continue
        if item.get("name") == "ray":
            ray = item
            break
    if ray is None:
        if containers:
            # Preserve user sidecars and let validation report the missing
            # `ray` container.  Inserting one here would silently "repair" an
            # invalid spec and make `validate` useless.
            return None
        # An entirely empty template gets a usable default for first-run UX.
        ray = container("ray", image)
        containers.insert(0, ray)
    ray.setdefault("image", image)
    if not ray.get("resources"):
        ray["resources"] = {
            "requests": {"cpu": "500m", "memory": "512Mi"},
            "limits": {"cpu": "1", "memory": "1Gi"},
        }
    return ray


def default_raycluster(obj: dict) -> dict:
    spec = _ensure_spec(obj)
    spec.setdefault("rayVersion", C.DEFAULT_RAY_VERSION)
    spec.setdefault("suspend", False)

    head = spec.get("headGroupSpec")
    if not isinstance(head, dict):
        # Leave the malformed value for validation to report; replacing it
        # here would hide a type error from the user.
        return obj
    head.setdefault("rayStartParams", {})
    head.setdefault("serviceType", "ClusterIP")
    head_template = head.setdefault("template", {})
    _default_ray_container(head_template, C.DEFAULT_IMAGE)
    head_template.setdefault("metadata", {}).setdefault("labels", {})

    if "enableInTreeAutoscaling" not in spec:
        spec["enableInTreeAutoscaling"] = False
    if spec.get("enableInTreeAutoscaling"):
        options = spec.setdefault("autoscalerOptions", {})
        if not isinstance(options, dict):
            return obj
        options.setdefault("idleTimeoutSeconds", C.DEFAULT_IDLE_TIMEOUT_SECONDS)
        options.setdefault("upscalingMode", "Default")
        if "version" not in options:
            options["version"] = "v2" if ray_supports_autoscaler_v2(spec.get("rayVersion", "")) else "v1"

    worker_groups = spec.get("workerGroupSpecs")
    if not isinstance(worker_groups, list):
        # Same as headGroupSpec: preserve the invalid value for validation.
        return obj
    for index, group in enumerate(worker_groups):
        if not isinstance(group, dict):
            continue
        group.setdefault("groupName", f"worker-group-{index}")
        group.setdefault("replicas", 0)
        group.setdefault("minReplicas", 0)
        group.setdefault("maxReplicas", C.MAX_INT32)
        group.setdefault("numOfHosts", 1)
        group.setdefault("priority", 0)
        group.setdefault("rayStartParams", {})
        group.setdefault("scaleStrategy", {})
        if not isinstance(group.get("template"), dict):
            group["template"] = {}
        _default_ray_container(group["template"], C.DEFAULT_IMAGE)
        if spec.get("enableInTreeAutoscaling") and group.get("idleTimeoutSeconds") is None:
            group["idleTimeoutSeconds"] = spec.get("autoscalerOptions", {}).get(
                "idleTimeoutSeconds", C.DEFAULT_IDLE_TIMEOUT_SECONDS
            )

    if spec.get("gcsFaultToleranceOptions") and isinstance(spec["gcsFaultToleranceOptions"], dict):
        spec["gcsFaultToleranceOptions"].setdefault("backend", "redis")
    if spec.get("tlsOptions") and isinstance(spec["tlsOptions"], dict):
        spec["tlsOptions"].setdefault("enabled", False)
    return obj


def default_rayjob(obj: dict) -> dict:
    spec = _ensure_spec(obj)
    spec.setdefault("submissionMode", "K8sJobMode")
    spec.setdefault("backoffLimit", 0)
    spec.setdefault("ttlSecondsAfterFinished", 0)
    spec.setdefault("shutdownAfterJobFinishes", False)
    spec.setdefault("suspend", False)
    spec.setdefault("metadata", {})
    # KubeRay's submitter Job defaults to 2 retries; this is different from the
    # RayJob-level backoffLimit (default 0).  SidecarMode has no submitter Job,
    # so it must not receive a defaulted submitterConfig (validation rejects it).
    if spec.get("submissionMode") != "SidecarMode":
        submitter = spec.setdefault("submitterConfig", {})
        if isinstance(submitter, dict):
            submitter.setdefault("backoffLimit", 2)
    if isinstance(spec.get("rayClusterSpec"), dict):
        # A RayJob's cluster template is defaulted exactly like a RayCluster.
        wrapper = {"metadata": {"name": obj.get("metadata", {}).get("name", "rayjob")}, "spec": spec["rayClusterSpec"]}
        default_raycluster(wrapper)
        spec["rayClusterSpec"] = wrapper["spec"]
    return obj


def default_rayservice(obj: dict) -> dict:
    spec = _ensure_spec(obj)
    spec.setdefault("suspend", False)
    spec.setdefault("excludeHeadPodFromServeSvc", False)
    spec.setdefault("rayClusterDeletionDelaySeconds", C.DEFAULT_RAYCLUSTER_DELETION_DELAY_SECONDS)
    cluster_spec = spec.get("rayClusterConfig")
    if not isinstance(cluster_spec, dict):
        # Preserve malformed input for the validator.
        cluster_spec = None
    if cluster_spec is not None:
        wrapper = {"metadata": {"name": obj.get("metadata", {}).get("name", "rayservice")}, "spec": cluster_spec}
        default_raycluster(wrapper)
        spec["rayClusterConfig"] = wrapper["spec"]

    strategy = spec.setdefault("upgradeStrategy", {})
    if not isinstance(strategy, dict):
        return obj
    strategy.setdefault("type", "NewCluster")
    if strategy.get("type") == "NewClusterWithIncrementalUpgrade":
        options = strategy.setdefault("clusterUpgradeOptions", {})
        options.setdefault("maxSurgePercent", 100)
        options.setdefault("stepSizePercent", 10)
        options.setdefault("intervalSeconds", 10)
        options.setdefault("gatewayClassName", "")
    return obj


def default_raycronjob(obj: dict) -> dict:
    spec = _ensure_spec(obj)
    spec.setdefault("suspend", False)
    template = spec.get("jobTemplate")
    if not isinstance(template, dict):
        # Keep the invalid shape so validation can explain the problem.
        return obj
    wrapper = {"metadata": {"name": obj.get("metadata", {}).get("name", "raycronjob")}, "spec": template}
    default_rayjob(wrapper)
    spec["jobTemplate"] = wrapper["spec"]
    return obj


def default_object(obj: dict) -> dict:
    kind = obj.get("kind")
    if kind == C.KIND_RAYCLUSTER:
        return default_raycluster(obj)
    if kind == C.KIND_RAYJOB:
        return default_rayjob(obj)
    if kind == C.KIND_RAYSERVICE:
        return default_rayservice(obj)
    if kind == C.KIND_RAYCRONJOB:
        return default_raycronjob(obj)
    return obj


def apply_defaults(obj: dict) -> dict:
    """Return a defaulted deep copy; never mutate the caller's object."""
    return default_object(deep_copy(obj))
