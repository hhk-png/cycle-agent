"""Admission control: defaulting, validation, LimitRange, ResourceQuota, PSA.

The chain runs inside the API server before an object is persisted.  It is the
same conceptual slot as Kubernetes' mutating and validating admission
controllers, although real clusters can also call out to webhooks.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable

from mini_k8s.apimachinery.cron import parse_cron
from mini_k8s.apimachinery.meta import deep_copy, validate_name, validate_namespace
from mini_k8s.apimachinery.quantity import parse_quantity
from mini_k8s.apimachinery.selectors import match_labels
from mini_k8s.errors import forbidden, invalid

CONTAINER_DEFAULTS = {
    "imagePullPolicy": "IfNotPresent",
    "terminationMessagePath": "/dev/termination-log",
    "terminationMessagePolicy": "File",
}


@dataclass
class AdmissionRequest:
    operation: str
    gvr: tuple[str, str, str]
    kind: str
    obj: dict
    old_obj: dict | None = None
    namespace: str | None = None
    name: str | None = None
    username: str = "system:anonymous"
    groups: list[str] | None = None


class AdmissionChain:
    def __init__(self, store, registry):
        self.store = store
        self.registry = registry
        self.validators: list[Callable[[AdmissionRequest], None]] = []

    def register_validator(self, validator: Callable[[AdmissionRequest], None]) -> None:
        self.validators.append(validator)

    def admit(self, request: AdmissionRequest) -> dict:
        obj = deep_copy(request.obj)
        kind = request.kind
        if request.operation in ("CREATE", "UPDATE"):
            self._check_namespace(request, obj)
            self._default(kind, obj, request)
            self._default_limit_range(request, obj)
            self._validate(kind, obj, request)
            self._pod_security(request, obj)
            self._custom_resources(request, obj)
            self._quota(request, obj)
            for validator in self.validators:
                validator(request)
        return obj

    # --------------------------------------------------------------- namespace
    def _check_namespace(self, request: AdmissionRequest, obj: dict) -> None:
        if request.gvr[2] == "namespaces":
            return
        if request.namespace and not self.store.get(("", "v1", "namespaces"), "", request.namespace):
            raise invalid(request.kind, request.name or "", f'namespace "{request.namespace}" not found')

    # ----------------------------------------------------------------- defaults
    def _default(self, kind: str, obj: dict, request: AdmissionRequest) -> None:
        meta = obj.setdefault("metadata", {})
        meta.setdefault("labels", {})
        meta.setdefault("annotations", {})
        meta.setdefault("finalizers", [])
        if request.namespace is not None:
            meta["namespace"] = request.namespace
        if kind == "Namespace":
            meta.setdefault("labels", {})["kubernetes.io/metadata.name"] = meta.get("name", "")
            finalizers = meta.setdefault("finalizers", [])
            if "kubernetes" not in finalizers:
                finalizers.append("kubernetes")
        elif kind == "CustomResourceDefinition":
            finalizers = meta.setdefault("finalizers", [])
            if "customresourcecleanup.apiextensions.k8s.io" not in finalizers:
                finalizers.append("customresourcecleanup.apiextensions.k8s.io")
        elif kind == "Pod":
            if request.operation == "CREATE":
                obj.setdefault("status", {"phase": "Pending"})
            self._default_pod(obj)
        elif kind in ("Deployment", "ReplicaSet", "StatefulSet", "DaemonSet", "Job"):
            self._default_workload(kind, obj)
        elif kind == "CronJob":
            self._default_cronjob(obj)
        elif kind == "Service":
            self._default_service(obj)
        elif kind == "PersistentVolumeClaim":
            spec = obj.setdefault("spec", {})
            spec.setdefault("volumeMode", "Filesystem")
        elif kind == "PersistentVolume":
            spec = obj.setdefault("spec", {})
            spec.setdefault("volumeMode", "Filesystem")
            spec.setdefault("persistentVolumeReclaimPolicy", "Retain")
        elif kind == "Ingress":
            obj.setdefault("spec", {}).setdefault("ingressClassName", "mini-nginx")
            for rule in obj.get("spec", {}).get("rules", []):
                for path in rule.get("http", {}).get("paths", []):
                    path.setdefault("pathType", "Prefix")
        elif kind == "Gateway":
            # Upstream defaults are implementation-specific.  MiniK8s accepts
            # routes only from the same namespace unless the listener says
            # otherwise, which is the safest useful default.
            for listener in obj.get("spec", {}).get("listeners", []) or []:
                allowed = listener.get("allowedRoutes")
                if not isinstance(allowed, dict):
                    allowed = {}
                listener["allowedRoutes"] = allowed
                namespaces = allowed.get("namespaces")
                if not isinstance(namespaces, dict):
                    namespaces = {}
                namespaces.setdefault("from", "Same")
                allowed["namespaces"] = namespaces
        elif kind == "HTTPRoute":
            spec = obj.setdefault("spec", {})
            spec.setdefault("rules", [])
            spec.setdefault("hostnames", [])
        elif kind == "HorizontalPodAutoscaler":
            spec = obj.setdefault("spec", {})
            spec.setdefault("minReplicas", 1)
            spec.setdefault("maxReplicas", 10)
            if not spec.get("metrics"):
                spec["metrics"] = [{"type": "Resource", "resource": {
                    "name": "cpu", "target": {"type": "Utilization", "averageUtilization": 50}}}]
        elif kind == "Job":
            self._default_job(obj)
        elif kind == "PodDisruptionBudget":
            obj.setdefault("spec", {}).setdefault("unhealthyPodEvictionPolicy", "IfHealthyBudget")

    def _default_pod(self, obj: dict) -> None:
        spec = obj.setdefault("spec", {})
        spec.setdefault("restartPolicy", "Always")
        spec.setdefault("dnsPolicy", "ClusterFirst")
        spec.setdefault("terminationGracePeriodSeconds", 30)
        spec.setdefault("serviceAccountName", "default")
        spec.setdefault("enableServiceLinks", True)
        spec.setdefault("schedulerName", "mini-scheduler")
        for container in spec.get("initContainers", []) + spec.get("containers", []):
            for key, value in CONTAINER_DEFAULTS.items():
                container.setdefault(key, value)
            container.setdefault("resources", {})
            self._default_container_resources(container)
            for probe_name in ("livenessProbe", "readinessProbe", "startupProbe"):
                probe = container.get(probe_name)
                if probe:
                    probe.setdefault("initialDelaySeconds", 0)
                    probe.setdefault("periodSeconds", 10)
                    probe.setdefault("timeoutSeconds", 1)
                    probe.setdefault("successThreshold", 1)
                    probe.setdefault("failureThreshold", 3)
        if spec.get("priorityClassName"):
            priority_class = self.store.get(("scheduling.k8s.io", "v1", "priorityclasses"), "", spec["priorityClassName"])
            if priority_class:
                spec["priority"] = priority_class.get("value", 0)
                # preemptionPolicy is inherited from the PriorityClass just
                # like priority.  A pod-level override still wins.
                spec.setdefault("preemptionPolicy", priority_class.get("preemptionPolicy", "PreemptLowerPriority"))
        elif "priority" not in spec:
            spec["priority"] = 0

    @staticmethod
    def _default_container_resources(container: dict) -> None:
        """Apply Kubernetes' request/limit defaulting rules.

        A limit without a request defaults the request to the limit (which is
        also what makes a limits-only Pod Guaranteed).  Extended resources are
        stricter: they must be integral and request must equal limit, so a
        request-only extended resource defaults its limit from the request.
        """
        resources = container.setdefault("resources", {})
        requests = resources.get("requests") or {}
        limits = resources.get("limits") or {}
        if limits:
            requests = dict(requests)
            for key, value in limits.items():
                requests.setdefault(key, value)
            resources["requests"] = requests
        if requests:
            for key, value in requests.items():
                if "/" not in key:
                    continue
                limits = dict(resources.get("limits") or {})
                limits.setdefault(key, value)
                resources["limits"] = limits

    def _default_workload(self, kind: str, obj: dict) -> None:
        spec = obj.setdefault("spec", {})
        if kind in ("Deployment", "ReplicaSet", "StatefulSet"):
            spec.setdefault("replicas", 1)
        template = spec.setdefault("template", {"metadata": {"labels": {}}, "spec": {"containers": []}})
        template.setdefault("metadata", {}).setdefault("labels", {})
        if not template["metadata"]["labels"]:
            template["metadata"]["labels"] = {"app": obj.get("metadata", {}).get("name", "")}
        if kind == "Deployment":
            spec.setdefault("strategy", {"type": "RollingUpdate", "rollingUpdate": {"maxSurge": "25%", "maxUnavailable": "25%"}})
            spec.setdefault("revisionHistoryLimit", 10)
            spec.setdefault("progressDeadlineSeconds", 600)
            if not spec.get("selector"):
                spec["selector"] = {"matchLabels": deep_copy(template["metadata"]["labels"])}
        elif kind == "ReplicaSet":
            if not spec.get("selector"):
                spec["selector"] = {"matchLabels": deep_copy(template["metadata"]["labels"])}
        elif kind == "StatefulSet":
            spec.setdefault("serviceName", obj.get("metadata", {}).get("name", ""))
            spec.setdefault("podManagementPolicy", "OrderedReady")
            spec.setdefault("updateStrategy", {"type": "RollingUpdate", "rollingUpdate": {"partition": 0}})
            spec.setdefault("revisionHistoryLimit", 10)
            if not spec.get("selector"):
                spec["selector"] = {"matchLabels": deep_copy(template["metadata"]["labels"])}
        elif kind == "DaemonSet":
            spec.setdefault("updateStrategy", {"type": "RollingUpdate", "rollingUpdate": {"maxUnavailable": 1}})
            if not spec.get("selector"):
                spec["selector"] = {"matchLabels": deep_copy(template["metadata"]["labels"])}
        if kind == "Job":
            self._default_job(obj)
        if "containers" not in template.setdefault("spec", {}):
            template["spec"]["containers"] = []

    def _default_job(self, obj: dict) -> None:
        spec = obj.setdefault("spec", {})
        spec.setdefault("completions", 1)
        spec.setdefault("parallelism", 1)
        spec.setdefault("backoffLimit", 6)
        spec.setdefault("completionMode", "NonIndexed")
        spec.setdefault("suspend", False)
        template = spec.get("template", {})
        pod_spec = template.setdefault("spec", {})
        pod_spec.setdefault("restartPolicy", "Never")

    def _default_cronjob(self, obj: dict) -> None:
        spec = obj.setdefault("spec", {})
        spec.setdefault("concurrencyPolicy", "Allow")
        spec.setdefault("suspend", False)
        spec.setdefault("successfulJobsHistoryLimit", 3)
        spec.setdefault("failedJobsHistoryLimit", 1)
        job_template = spec.setdefault("jobTemplate", {"spec": {"template": {"spec": {}}}})
        job_spec = job_template.setdefault("spec", {})
        job_spec.setdefault("completions", 1)
        job_spec.setdefault("parallelism", 1)
        job_spec.setdefault("backoffLimit", 6)
        job_spec.setdefault("template", {"spec": {"restartPolicy": "Never", "containers": []}})
        job_spec["template"].setdefault("spec", {}).setdefault("restartPolicy", "Never")

    def _default_service(self, obj: dict) -> None:
        spec = obj.setdefault("spec", {})
        spec.setdefault("type", "ClusterIP")
        spec.setdefault("sessionAffinity", "None")
        for port in spec.get("ports", []):
            port.setdefault("protocol", "TCP")
            port.setdefault("targetPort", port.get("port"))
            port.setdefault("name", f"port-{port.get('port')}" if len(spec.get("ports", [])) > 1 else "")
        if spec.get("type") == "ExternalName":
            spec.setdefault("clusterIP", "")

    # ------------------------------------------------------------------- limits
    def _default_limit_range(self, request: AdmissionRequest, obj: dict) -> None:
        if request.kind != "Pod" or not request.namespace:
            return
        ranges = self.store.list(("", "v1", "limitranges"), request.namespace)[0]
        for limit_range in ranges:
            for item in limit_range.get("spec", {}).get("limits", []):
                if item.get("type") != "Container":
                    continue
                for container in obj.get("spec", {}).get("containers", []):
                    resources = container.setdefault("resources", {})
                    requests = resources.setdefault("requests", {})
                    limits = resources.setdefault("limits", {})
                    for key, value in item.get("defaultRequest", {}).items():
                        requests.setdefault(key, value)
                    for key, value in item.get("default", {}).items():
                        limits.setdefault(key, value)
                    for key in list(limits):
                        requests.setdefault(key, limits[key])

    def _validate_limit_range(self, obj: dict, request: AdmissionRequest) -> None:
        """Enforce LimitRange min/max/maxLimitRequestRatio for containers."""
        if not request.namespace:
            return
        name = obj.get("metadata", {}).get("name", "")
        ranges = self.store.list(("", "v1", "limitranges"), request.namespace)[0]
        for limit_range in ranges:
            for item in limit_range.get("spec", {}).get("limits", []):
                if item.get("type") != "Container":
                    continue
                for container in obj.get("spec", {}).get("containers", []) + obj.get("spec", {}).get("initContainers", []):
                    resources = container.get("resources", {})
                    requests = resources.get("requests", {})
                    limits = resources.get("limits", {})
                    for key, minimum in item.get("min", {}).items():
                        for label, values in (("request", requests), ("limit", limits)):
                            if key in values and parse_quantity(values[key]) < parse_quantity(minimum):
                                raise invalid("Pod", name,
                                              f"container {container.get('name')} {label} {key}={values[key]} "
                                              f"is below LimitRange minimum {minimum}")
                    for key, maximum in item.get("max", {}).items():
                        for label, values in (("request", requests), ("limit", limits)):
                            if key in values and parse_quantity(values[key]) > parse_quantity(maximum):
                                raise invalid("Pod", name,
                                              f"container {container.get('name')} {label} {key}={values[key]} "
                                              f"exceeds LimitRange maximum {maximum}")
                    for key, ratio in item.get("maxLimitRequestRatio", {}).items():
                        if key in requests and key in limits and parse_quantity(requests[key]) > 0:
                            actual = parse_quantity(limits[key]) / parse_quantity(requests[key])
                            if actual > parse_quantity(ratio):
                                raise invalid("Pod", name,
                                              f"container {container.get('name')} {key} limit/request ratio "
                                              f"{actual:g} exceeds LimitRange maximum {ratio}")

    # --------------------------------------------------------------- validation
    def _validate(self, kind: str, obj: dict, request: AdmissionRequest) -> None:
        meta = obj.get("metadata", {})
        name = meta.get("name", "")
        if not name:
            raise invalid(kind, "", "metadata.name is required")
        validate_name(name, kind)
        if request.namespace:
            validate_namespace(request.namespace)
        if kind == "Pod":
            self._validate_pod(obj)
            self._validate_limit_range(obj, request)
        elif kind in ("Deployment", "ReplicaSet", "StatefulSet", "DaemonSet"):
            self._validate_workload(kind, obj)
        elif kind == "Job":
            self._validate_job(obj)
        elif kind == "CronJob":
            schedule = obj.get("spec", {}).get("schedule", "")
            if not schedule:
                raise invalid(kind, name, "spec.schedule is required")
            try:
                parse_cron(schedule)
            except ValueError as exc:
                raise invalid(kind, name, str(exc)) from exc
            job_spec = obj.get("spec", {}).get("jobTemplate", {}).get("spec", {})
            self._validate_job({"metadata": {"name": name}, "spec": job_spec}, kind=kind)
        elif kind == "Service":
            self._validate_service(obj)
        elif kind == "PersistentVolumeClaim":
            if not obj.get("spec", {}).get("accessModes"):
                raise invalid(kind, name, "spec.accessModes is required")
        elif kind == "ResourceQuota":
            self._validate_quota(obj)
        elif kind == "HorizontalPodAutoscaler":
            spec = obj.get("spec", {})
            if spec.get("minReplicas", 1) > spec.get("maxReplicas", 1):
                raise invalid(kind, name, "minReplicas cannot be greater than maxReplicas")
            if not spec.get("scaleTargetRef", {}).get("name"):
                raise invalid(kind, name, "spec.scaleTargetRef.name is required")
        elif kind == "Ingress":
            rules = obj.get("spec", {}).get("rules", [])
            for rule in rules:
                for path in rule.get("http", {}).get("paths", []):
                    if not path.get("backend"):
                        raise invalid(kind, name, "every ingress path requires a backend")

    def _validate_pod(self, obj: dict) -> None:
        spec = obj.get("spec", {})
        containers = spec.get("containers", [])
        if not containers:
            raise invalid("Pod", obj.get("metadata", {}).get("name", ""), "spec.containers must not be empty")
        names: set[str] = set()
        for container in containers:
            cname = container.get("name", "")
            if not cname:
                raise invalid("Pod", obj.get("metadata", {}).get("name", ""), "container name is required")
            if cname in names:
                raise invalid("Pod", obj.get("metadata", {}).get("name", ""), f"duplicate container name {cname!r}")
            names.add(cname)
            if not container.get("image"):
                raise invalid("Pod", obj.get("metadata", {}).get("name", ""), f"container {cname!r} has no image")
            self._validate_resources(container, "Pod", obj.get("metadata", {}).get("name", ""))
        for container in spec.get("initContainers", []):
            if container.get("name") in names:
                raise invalid("Pod", obj.get("metadata", {}).get("name", ""), "init container names must be unique across containers")
            names.add(container.get("name", ""))
        restart = spec.get("restartPolicy", "Always")
        if restart not in ("Always", "OnFailure", "Never"):
            raise invalid("Pod", obj.get("metadata", {}).get("name", ""), f"invalid restartPolicy {restart!r}")
        for volume in spec.get("volumes", []):
            if "name" not in volume:
                raise invalid("Pod", obj.get("metadata", {}).get("name", ""), "volume name is required")
        pid = spec.get("securityContext", {}).get("runAsNonRoot")
        if pid is not None and not isinstance(pid, bool):
            raise invalid("Pod", obj.get("metadata", {}).get("name", ""), "runAsNonRoot must be a boolean")

    def _validate_resources(self, container: dict, kind: str, name: str) -> None:
        resources = container.get("resources", {})
        requests = resources.get("requests", {})
        limits = resources.get("limits", {})
        parsed: dict[tuple[str, str], float] = {}
        for section, values in (("request", requests), ("limit", limits)):
            for key, value in values.items():
                try:
                    parsed[(section, key)] = parse_quantity(value)
                except ValueError as exc:
                    raise invalid(kind, name, str(exc)) from exc
        for key in set(requests) | set(limits):
            request = parsed.get(("request", key))
            limit = parsed.get(("limit", key))
            if "/" in key:
                # Extended resources (for example nvidia.com/gpu) are integer
                # counts and must declare request == limit.  Admission
                # defaulting above has already filled the missing side.
                if request is None or limit is None:
                    raise invalid(kind, name, f"extended resource {key!r} must set both requests and limits")
                if request != limit:
                    raise invalid(kind, name, f"extended resource {key!r} request must equal limit")
                if request != int(request):
                    raise invalid(kind, name, f"extended resource {key!r} must be an integer")
                if request < 0:
                    raise invalid(kind, name, f"extended resource {key!r} must be non-negative")
            elif request is not None and limit is not None and request > limit:
                raise invalid(kind, name, f"container {container.get('name')!r} request for {key} exceeds limit")

    def _validate_workload(self, kind: str, obj: dict) -> None:
        spec = obj.get("spec", {})
        name = obj.get("metadata", {}).get("name", "")
        selector = spec.get("selector", {})
        if not selector:
            raise invalid(kind, name, "spec.selector is required")
        template_labels = spec.get("template", {}).get("metadata", {}).get("labels", {})
        if selector.get("matchLabels"):
            if not match_labels(template_labels, ",".join(f"{k}={v}" for k, v in selector["matchLabels"].items())):
                raise invalid(kind, name, "spec.selector does not match template labels")
        pod_spec = spec.get("template", {}).get("spec", {})
        if not pod_spec.get("containers"):
            raise invalid(kind, name, "template.spec.containers must not be empty")
        if pod_spec.get("restartPolicy", "Always") != "Always":
            raise invalid(kind, name, "restartPolicy must be Always for long-running workloads")
        if kind == "StatefulSet" and not spec.get("serviceName"):
            raise invalid(kind, name, "spec.serviceName is required")

    def _validate_job(self, obj: dict, *, kind: str = "Job") -> None:
        spec = obj.get("spec", {})
        name = obj.get("metadata", {}).get("name", "")
        if spec.get("parallelism", 1) < 0 or spec.get("completions", 1) < 0:
            raise invalid(kind, name, "parallelism and completions must be non-negative")
        restart = spec.get("template", {}).get("spec", {}).get("restartPolicy", "Never")
        if restart not in ("Never", "OnFailure"):
            raise invalid(kind, name, "Job pods must use restartPolicy Never or OnFailure")
        if not spec.get("template", {}).get("spec", {}).get("containers"):
            raise invalid(kind, name, "job template must define containers")

    def _validate_service(self, obj: dict) -> None:
        spec = obj.get("spec", {})
        name = obj.get("metadata", {}).get("name", "")
        if spec.get("type") not in ("ClusterIP", "NodePort", "LoadBalancer", "ExternalName"):
            raise invalid("Service", name, f"unknown service type {spec.get('type')!r}")
        if spec.get("type") == "ExternalName" and not spec.get("externalName"):
            raise invalid("Service", name, "externalName is required for ExternalName services")
        ports = spec.get("ports", [])
        if spec.get("type") != "ExternalName" and not ports:
            raise invalid("Service", name, "spec.ports must not be empty")
        seen: set[tuple[str, int]] = set()
        for port in ports:
            key = (port.get("protocol", "TCP"), int(port.get("port", 0)))
            if key in seen:
                raise invalid("Service", name, f"duplicate port {key[1]}/{key[0]}")
            seen.add(key)
            if not (1 <= int(port.get("port", 0)) <= 65535):
                raise invalid("Service", name, "port must be between 1 and 65535")

    def _validate_quota(self, obj: dict) -> None:
        hard = obj.get("spec", {}).get("hard", {})
        for key, value in hard.items():
            try:
                parse_quantity(value)
            except ValueError as exc:
                raise invalid("ResourceQuota", obj.get("metadata", {}).get("name", ""), str(exc)) from exc

    # ------------------------------------------------------------ pod security
    def _pod_security(self, request: AdmissionRequest, obj: dict) -> None:
        if request.kind != "Pod" or not request.namespace:
            return
        namespace = self.store.get(("", "v1", "namespaces"), "", request.namespace)
        if not namespace:
            return
        level = namespace.get("metadata", {}).get("labels", {}).get("pod-security.kubernetes.io/enforce")
        if not level or level not in ("baseline", "restricted"):
            return
        spec = obj.get("spec", {})
        name = obj.get("metadata", {}).get("name", "")
        if level == "baseline":
            if spec.get("hostNetwork") or spec.get("hostPID") or spec.get("hostIPC"):
                raise forbidden(f"pod {name} violates baseline Pod Security: host namespaces are forbidden")
            for volume in spec.get("volumes", []):
                if "hostPath" in volume:
                    raise forbidden(f"pod {name} violates baseline Pod Security: hostPath volumes are forbidden")
            for container in spec.get("containers", []) + spec.get("initContainers", []):
                ctx = container.get("securityContext", {})
                if ctx.get("privileged"):
                    raise forbidden(f"pod {name} violates baseline Pod Security: privileged containers are forbidden")
            return
        # restricted: the most common controls, not the full PSS profile.
        if spec.get("hostNetwork") or spec.get("hostPID") or spec.get("hostIPC"):
            raise forbidden(f"pod {name} violates restricted Pod Security: host namespaces are forbidden")
        pod_non_root = spec.get("securityContext", {}).get("runAsNonRoot") is True
        for container in spec.get("containers", []) + spec.get("initContainers", []):
            ctx = container.get("securityContext", {})
            if ctx.get("runAsNonRoot") is False:
                raise forbidden(f"pod {name} violates restricted Pod Security: runAsNonRoot must not be disabled")
            if not pod_non_root and ctx.get("runAsNonRoot") is not True:
                raise forbidden(f"pod {name} violates restricted Pod Security: runAsNonRoot must be true")
            if ctx.get("runAsUser") == 0:
                raise forbidden(f"pod {name} violates restricted Pod Security: runAsUser 0 is forbidden")
            if ctx.get("privileged") or ctx.get("allowPrivilegeEscalation") is not False:
                raise forbidden(f"pod {name} violates restricted Pod Security: privilege escalation must be disabled")
            caps = ctx.get("capabilities", {}).get("drop", [])
            if "ALL" not in caps:
                raise forbidden(f"pod {name} violates restricted Pod Security: drop ALL capabilities")
            seccomp = ctx.get("seccompProfile", {}).get("type") or spec.get("securityContext", {}).get("seccompProfile", {}).get("type")
            if seccomp not in ("RuntimeDefault", "Localhost"):
                raise forbidden(f"pod {name} violates restricted Pod Security: seccompProfile is required")
        for volume in spec.get("volumes", []):
            if "hostPath" in volume:
                raise forbidden(f"pod {name} violates restricted Pod Security: hostPath volumes are forbidden")

    # -------------------------------------------------------------------- CRDs
    def _custom_resources(self, request: AdmissionRequest, obj: dict) -> None:
        group, version, plural = request.gvr
        if not group or group in ("apps", "batch", "autoscaling", "networking.k8s.io", "storage.k8s.io",
                                  "rbac.authorization.k8s.io", "policy", "scheduling.k8s.io",
                                  "apiextensions.k8s.io", "coordination.k8s.io", "discovery.k8s.io",
                                  "admissionregistration.k8s.io", "metrics.k8s.io"):
            return
        if plural == "customresourcedefinitions":
            return
        crd = self.store.get(("apiextensions.k8s.io", "v1", "customresourcedefinitions"), "", f"{plural}.{group}")
        if not crd:
            return
        schema = None
        for candidate in crd.get("spec", {}).get("versions", []):
            if candidate.get("name") == version:
                schema = candidate.get("schema", {}).get("openAPIV3Schema")
                break
        if schema:
            errors = validate_openapi(obj, schema)
            if errors:
                raise invalid(request.kind, request.name or "", "; ".join(errors[:5]))

    # ------------------------------------------------------------------- quota
    def _quota(self, request: AdmissionRequest, obj: dict) -> None:
        if not request.namespace:
            return
        quotas = self.store.list(("", "v1", "resourcequotas"), request.namespace)[0]
        if not quotas:
            return
        for quota in quotas:
            hard = quota.get("spec", {}).get("hard", {})
            if not hard:
                continue
            usage = quota_usage(self.store, request.namespace)
            contribution = object_quota_usage(request.kind, obj, request.gvr)
            if request.operation == "UPDATE" and request.old_obj:
                old_contribution = object_quota_usage(request.kind, request.old_obj, request.gvr)
            else:
                old_contribution = {}
            for key, limit in hard.items():
                used = usage.get(key, 0.0) - old_contribution.get(key, 0.0) + contribution.get(key, 0.0)
                try:
                    allowed = parse_quantity(limit)
                except ValueError:
                    continue
                if used > allowed + 1e-9:
                    raise forbidden(
                        f"exceeded quota: {quota['metadata']['name']}, requested: {key}={contribution.get(key, 0)}, "
                        f"used: {usage.get(key, 0)}, limited: {limit}"
                    )


def _pod_resource_totals(spec: dict, resource_key: str) -> dict[str, float]:
    """Effective Pod request/limit: max(max init container, sum app containers)."""
    app: dict[str, float] = {}
    for container in spec.get("containers", []):
        for key, value in container.get("resources", {}).get(resource_key, {}).items():
            app[key] = app.get(key, 0.0) + parse_quantity(value)
    init: dict[str, float] = {}
    for container in spec.get("initContainers", []):
        for key, value in container.get("resources", {}).get(resource_key, {}).items():
            init[key] = max(init.get(key, 0.0), parse_quantity(value))
    return {key: max(app.get(key, 0.0), init.get(key, 0.0)) for key in set(app) | set(init)}


def object_quota_usage(kind: str, obj: dict, gvr: tuple[str, str, str]) -> dict[str, float]:
    usage: dict[str, float] = {}
    if kind == "Pod":
        usage["pods"] = 1
        requests = _pod_resource_totals(obj.get("spec", {}), "requests")
        limits = _pod_resource_totals(obj.get("spec", {}), "limits")
        for key, value in requests.items():
            usage[f"requests.{key}"] = value
        for key, value in limits.items():
            usage[f"limits.{key}"] = value
        # cpu/memory aliases used by older quota manifests.
        if "cpu" in requests:
            usage["cpu"] = requests["cpu"]
        if "memory" in requests:
            usage["memory"] = requests["memory"]
    elif kind in ("ConfigMap", "Secret", "Service", "PersistentVolumeClaim", "ResourceQuota", "ReplicationController"):
        usage[kind.lower() + "s"] = 1
    else:
        plural = gvr[2]
        group = gvr[0]
        usage[f"count/{plural}.{group}" if group else f"count/{plural}"] = 1
    return usage


def quota_usage(store, namespace: str) -> dict[str, float]:
    usage: dict[str, float] = {}
    pods = store.list(("", "v1", "pods"), namespace)[0]
    for pod in pods:
        if pod.get("status", {}).get("phase") in ("Succeeded", "Failed"):
            continue
        for key, value in object_quota_usage("Pod", pod, ("", "v1", "pods")).items():
            usage[key] = usage.get(key, 0.0) + value
    for kind, gvr, plural in (
        ("ConfigMap", ("", "v1", "configmaps"), "configmaps"),
        ("Secret", ("", "v1", "secrets"), "secrets"),
        ("Service", ("", "v1", "services"), "services"),
        ("PersistentVolumeClaim", ("", "v1", "persistentvolumeclaims"), "persistentvolumeclaims"),
        ("ResourceQuota", ("", "v1", "resourcequotas"), "resourcequotas"),
    ):
        count = store.count(gvr, namespace)
        if count:
            usage[plural] = count
    for definition in [
        ("apps", "v1", "deployments"), ("apps", "v1", "statefulsets"), ("apps", "v1", "daemonsets"),
        ("batch", "v1", "jobs"), ("batch", "v1", "cronjobs"), ("networking.k8s.io", "v1", "ingresses"),
        ("autoscaling", "v2", "horizontalpodautoscalers"),
    ]:
        count = store.count(definition, namespace)
        if count:
            group = definition[0]
            usage[f"count/{definition[2]}.{group}"] = count
    return usage


def validate_openapi(value: Any, schema: dict, path: str = "root") -> list[str]:
    """Validate a small but useful subset of OpenAPI v3 / structural schemas."""
    errors: list[str] = []
    if not isinstance(schema, dict):
        return errors
    expected = schema.get("type")
    if expected == "object":
        if not isinstance(value, dict):
            return [f"{path}: expected object"]
        for required in schema.get("required", []):
            if required not in value:
                errors.append(f"{path}.{required}: required field is missing")
        properties = schema.get("properties", {})
        for key, child in properties.items():
            if key in value:
                errors.extend(validate_openapi(value[key], child, f"{path}.{key}"))
        if schema.get("additionalProperties") is False:
            for key in value:
                if key not in properties:
                    errors.append(f"{path}.{key}: unknown field")
    elif expected == "array":
        if not isinstance(value, list):
            return [f"{path}: expected array"]
        item_schema = schema.get("items", {})
        for index, item in enumerate(value):
            errors.extend(validate_openapi(item, item_schema, f"{path}[{index}]"))
    elif expected == "string":
        if not isinstance(value, str):
            return [f"{path}: expected string"]
    elif expected in ("integer", "number"):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return [f"{path}: expected number"]
    elif expected == "boolean":
        if not isinstance(value, bool):
            return [f"{path}: expected boolean"]
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: must be one of {schema['enum']}")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path}: must be >= {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{path}: must be <= {schema['maximum']}")
    if "pattern" in schema and isinstance(value, str):
        try:
            if not re.match(schema["pattern"], value):
                errors.append(f"{path}: does not match pattern {schema['pattern']}")
        except re.error:
            pass
    return errors
