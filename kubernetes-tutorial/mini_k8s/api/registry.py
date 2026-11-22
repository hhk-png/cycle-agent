"""Resource registry and discovery metadata."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from mini_k8s.errors import bad_request, not_found


@dataclass(frozen=True)
class ResourceDefinition:
    group: str
    version: str
    plural: str
    kind: str
    namespaced: bool = True
    short_names: tuple[str, ...] = ()
    categories: tuple[str, ...] = ()
    verbs: tuple[str, ...] = ("create", "delete", "deletecollection", "get", "list", "patch", "update", "watch")
    has_status: bool = True
    has_scale: bool = False
    singular: str = ""
    aliases: tuple[str, ...] = field(default_factory=tuple)

    @property
    def api_version(self) -> str:
        return f"{self.group}/{self.version}" if self.group else self.version

    @property
    def gvr(self) -> tuple[str, str, str]:
        return self.group, self.version, self.plural

    @property
    def list_kind(self) -> str:
        return f"{self.kind}List"


def _r(group: str, version: str, plural: str, kind: str, *, namespaced: bool = True,
       short: Iterable[str] = (), categories: Iterable[str] = (), status: bool = True,
       scale: bool = False, singular: str = "") -> ResourceDefinition:
    return ResourceDefinition(
        group, version, plural, kind, namespaced, tuple(short), tuple(categories),
        has_status=status, has_scale=scale, singular=singular or kind.lower(),
    )


CORE_RESOURCES = [
    _r("", "v1", "namespaces", "Namespace", namespaced=False, short=("ns",), status=True),
    _r("", "v1", "nodes", "Node", namespaced=False, short=("no",), status=True),
    _r("", "v1", "pods", "Pod", short=("po",), categories=("all",)),
    _r("", "v1", "services", "Service", short=("svc",), categories=("all",)),
    _r("", "v1", "endpoints", "Endpoints", short=("ep",)),
    _r("", "v1", "configmaps", "ConfigMap", short=("cm",)),
    _r("", "v1", "secrets", "Secret", status=False),
    _r("", "v1", "serviceaccounts", "ServiceAccount", short=("sa",)),
    _r("", "v1", "persistentvolumes", "PersistentVolume", namespaced=False, short=("pv",)),
    _r("", "v1", "persistentvolumeclaims", "PersistentVolumeClaim", short=("pvc",)),
    _r("", "v1", "events", "Event", short=("ev",)),
    _r("", "v1", "resourcequotas", "ResourceQuota", short=("quota",)),
    _r("", "v1", "limitranges", "LimitRange", short=("limits",)),
    _r("", "v1", "bindings", "Binding", status=False),
    _r("", "v1", "podtemplates", "PodTemplate", status=False),
]

APP_RESOURCES = [
    _r("apps", "v1", "deployments", "Deployment", short=("deploy",), categories=("all",), scale=True),
    _r("apps", "v1", "replicasets", "ReplicaSet", short=("rs",), categories=("all",), scale=True),
    _r("apps", "v1", "statefulsets", "StatefulSet", short=("sts",), categories=("all",), scale=True),
    _r("apps", "v1", "daemonsets", "DaemonSet", short=("ds",), categories=("all",)),
    _r("apps", "v1", "controllerrevisions", "ControllerRevision", status=False),
]

BATCH_RESOURCES = [
    _r("batch", "v1", "jobs", "Job", categories=("all",), scale=True),
    _r("batch", "v1", "cronjobs", "CronJob", short=("cj",), categories=("all",)),
]

GATEWAY_RESOURCES = [
    # Gateway API keeps GatewayClass/Gateway/HTTPRoute in v1 while
    # ReferenceGrant is still served as v1beta1 in the upstream CRDs.
    _r("gateway.networking.k8s.io", "v1", "gatewayclasses", "GatewayClass",
       namespaced=False, short=("gc",)),
    _r("gateway.networking.k8s.io", "v1", "gateways", "Gateway", short=("gtw", "gw")),
    _r("gateway.networking.k8s.io", "v1", "httproutes", "HTTPRoute", short=("httproute",)),
    _r("gateway.networking.k8s.io", "v1beta1", "referencegrants", "ReferenceGrant",
       short=("refgrant",), status=False),
]

OTHER_RESOURCES = [
    _r("networking.k8s.io", "v1", "ingresses", "Ingress", short=("ing",)),
    _r("networking.k8s.io", "v1", "ingressclasses", "IngressClass", namespaced=False),
    _r("networking.k8s.io", "v1", "networkpolicies", "NetworkPolicy", short=("netpol",)),
    _r("discovery.k8s.io", "v1", "endpointslices", "EndpointSlice"),
    _r("storage.k8s.io", "v1", "storageclasses", "StorageClass", namespaced=False, short=("sc",)),
    _r("storage.k8s.io", "v1", "csidrivers", "CSIDriver", namespaced=False),
    _r("storage.k8s.io", "v1", "csinodes", "CSINode", namespaced=False),
    _r("rbac.authorization.k8s.io", "v1", "roles", "Role"),
    _r("rbac.authorization.k8s.io", "v1", "rolebindings", "RoleBinding"),
    _r("rbac.authorization.k8s.io", "v1", "clusterroles", "ClusterRole", namespaced=False),
    _r("rbac.authorization.k8s.io", "v1", "clusterrolebindings", "ClusterRoleBinding", namespaced=False),
    _r("autoscaling", "v2", "horizontalpodautoscalers", "HorizontalPodAutoscaler", short=("hpa",)),
    _r("policy", "v1", "poddisruptionbudgets", "PodDisruptionBudget", short=("pdb",)),
    _r("scheduling.k8s.io", "v1", "priorityclasses", "PriorityClass", namespaced=False, short=("pc",), status=False),
    _r("apiextensions.k8s.io", "v1", "customresourcedefinitions", "CustomResourceDefinition", namespaced=False, short=("crd", "crds"), status=True),
    _r("coordination.k8s.io", "v1", "leases", "Lease"),
    _r("admissionregistration.k8s.io", "v1", "validatingwebhookconfigurations", "ValidatingWebhookConfiguration", namespaced=False),
    _r("admissionregistration.k8s.io", "v1", "mutatingwebhookconfigurations", "MutatingWebhookConfiguration", namespaced=False),
    _r("metrics.k8s.io", "v1beta1", "nodes", "NodeMetrics", namespaced=False, status=False),
    _r("metrics.k8s.io", "v1beta1", "pods", "PodMetrics", status=False),
    _r("authorization.k8s.io", "v1", "selfsubjectaccessreviews", "SelfSubjectAccessReview", namespaced=False, status=False),
    _r("authorization.k8s.io", "v1", "subjectaccessreviews", "SubjectAccessReview", namespaced=False, status=False),
    _r("authorization.k8s.io", "v1", "selfsubjectrulesreviews", "SelfSubjectRulesReview", status=False),
    _r("authorization.k8s.io", "v1", "localsubjectaccessreviews", "LocalSubjectAccessReview", status=False),
    _r("authentication.k8s.io", "v1", "tokenreviews", "TokenReview", namespaced=False, status=False),
]

DEFAULT_RESOURCES = CORE_RESOURCES + APP_RESOURCES + BATCH_RESOURCES + GATEWAY_RESOURCES + OTHER_RESOURCES


class ResourceRegistry:
    def __init__(self, definitions: Iterable[ResourceDefinition] | None = None):
        self._definitions: dict[tuple[str, str, str], ResourceDefinition] = {}
        self._by_kind: dict[tuple[str, str], ResourceDefinition] = {}
        for definition in definitions if definitions is not None else DEFAULT_RESOURCES:
            self.register(definition)

    def register(self, definition: ResourceDefinition) -> None:
        self._definitions[definition.gvr] = definition
        self._by_kind[(definition.api_version, definition.kind)] = definition

    def unregister(self, definition: ResourceDefinition) -> None:
        self._definitions.pop(definition.gvr, None)
        self._by_kind.pop((definition.api_version, definition.kind), None)

    def get(self, group: str, version: str, plural: str) -> ResourceDefinition:
        try:
            return self._definitions[(group, version, plural)]
        except KeyError as exc:
            raise not_found("resource", f"{group}/{version}/{plural}") from exc

    def maybe_get(self, group: str, version: str, plural: str) -> ResourceDefinition | None:
        return self._definitions.get((group, version, plural))

    def by_kind(self, api_version: str, kind: str) -> ResourceDefinition:
        try:
            return self._by_kind[(api_version, kind)]
        except KeyError as exc:
            raise bad_request(f"no kind {kind!r} registered for {api_version!r}") from exc

    def for_group_version(self, group: str, version: str) -> list[ResourceDefinition]:
        return sorted(
            (d for d in self._definitions.values() if d.group == group and d.version == version),
            key=lambda d: d.plural,
        )

    def all(self) -> list[ResourceDefinition]:
        return sorted(self._definitions.values(), key=lambda d: (d.group, d.version, d.plural))

    def versions(self) -> dict[str, set[str]]:
        result: dict[str, set[str]] = {}
        for definition in self._definitions.values():
            result.setdefault(definition.group, set()).add(definition.version)
        return result

    def resolve_cli_name(self, token: str) -> ResourceDefinition:
        token = token.lower()
        for definition in self._definitions.values():
            if token in (definition.plural, definition.kind.lower(), definition.singular, *definition.short_names):
                return definition
        raise not_found("resource type", token)

    def from_crd(self, crd: dict) -> list[ResourceDefinition]:
        spec = crd.get("spec", {})
        group = spec.get("group", "")
        names = spec.get("names", {})
        plural = names.get("plural", "")
        kind = names.get("kind", "")
        if not group or not plural or not kind:
            raise bad_request("CRD must declare spec.group and spec.names.{plural,kind}")
        scope = spec.get("scope", "Namespaced")
        versions = spec.get("versions") or [{"name": spec.get("version"), "served": True}]
        result = []
        for version in versions:
            if not version.get("served", True):
                continue
            result.append(ResourceDefinition(
                group=group,
                version=version["name"],
                plural=plural,
                kind=kind,
                namespaced=scope != "Cluster",
                short_names=tuple(names.get("shortNames", [])),
                categories=tuple(names.get("categories", [])),
                has_scale="scale" in (version.get("subresources") or {}),
                singular=names.get("singular", kind.lower()),
            ))
        if not result:
            raise bad_request("CRD must serve at least one version")
        return result
