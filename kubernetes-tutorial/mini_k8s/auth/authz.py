"""RBAC authorization with cluster- and namespace-scoped bindings."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from mini_k8s.auth.authn import User, SYSTEM_MASTERS


@dataclass
class RequestAttributes:
    verb: str
    group: str = ""
    resource: str = ""
    namespace: str | None = None
    name: str | None = None
    subresource: str | None = None
    path: str | None = None
    non_resource: bool = False


def _subject_matches(subject: dict, user: User) -> bool:
    kind = subject.get("kind")
    if kind == "User":
        return subject.get("name") == user.name
    if kind == "Group":
        return subject.get("name") in user.groups
    if kind == "ServiceAccount":
        namespace = subject.get("namespace", "default")
        return user.name == f"system:serviceaccount:{namespace}:{subject.get('name')}"
    return False


def _rule_allows(rule: dict, attrs: RequestAttributes) -> bool:
    verbs = rule.get("verbs", [])
    if "*" not in verbs and attrs.verb not in verbs:
        return False
    if attrs.non_resource:
        for pattern in rule.get("nonResourceURLs", []):
            if pattern == "*" or attrs.path == pattern:
                return True
            if pattern.endswith("*") and (attrs.path or "").startswith(pattern[:-1]):
                return True
        return False
    groups = rule.get("apiGroups", [])
    if "*" not in groups and attrs.group not in groups:
        return False
    resources = rule.get("resources", [])
    requested = attrs.resource + (f"/{attrs.subresource}" if attrs.subresource else "")
    if "*" not in resources and requested not in resources:
        # A rule for the parent resource does not grant its subresources.
        return False
    resource_names = rule.get("resourceNames", [])
    if resource_names and attrs.name not in resource_names:
        return False
    return True


class RBACAuthorizer:
    """Reads ClusterRole/Binding objects straight from the store.

    In a real API server these objects are cached by an informer; reading them
    per request is slower but makes the data flow obvious.
    """

    CLUSTER_SCOPED = {
        ("rbac.authorization.k8s.io", "v1", "clusterroles"),
        ("rbac.authorization.k8s.io", "v1", "clusterrolebindings"),
    }

    def __init__(self, store):
        self.store = store
        self._cr_gvr = ("rbac.authorization.k8s.io", "v1", "clusterroles")
        self._crb_gvr = ("rbac.authorization.k8s.io", "v1", "clusterrolebindings")
        self._role_gvr = ("rbac.authorization.k8s.io", "v1", "roles")
        self._rb_gvr = ("rbac.authorization.k8s.io", "v1", "rolebindings")

    def _get_cluster_role(self, name: str) -> dict | None:
        return self.store.get(self._cr_gvr, "", name)

    def _get_role(self, namespace: str, name: str) -> dict | None:
        return self.store.get(self._role_gvr, namespace, name)

    def _role_for_ref(self, ref: dict, namespace: str) -> dict | None:
        if ref.get("kind") == "ClusterRole":
            return self._get_cluster_role(ref.get("name", ""))
        if ref.get("kind") == "Role":
            return self._get_role(namespace, ref.get("name", ""))
        return None

    def _role_rules_for_bindings(self, bindings: list[dict], user: User, namespace: str | None) -> list[dict]:
        rules: list[dict] = []
        for binding in bindings:
            if not any(_subject_matches(s, user) for s in binding.get("subjects", [])):
                continue
            role = self._role_for_ref(binding.get("roleRef", {}), namespace or binding.get("metadata", {}).get("namespace", ""))
            if role:
                rules.extend(role.get("rules", []))
        return rules

    def authorize(self, user: User, attrs: RequestAttributes) -> bool:
        if SYSTEM_MASTERS in user.groups:
            return True
        # Discovery is public, like Kubernetes' system:discovery binding.
        if attrs.non_resource and attrs.path in ("/api", "/apis", "/version", "/healthz", "/livez", "/readyz", "/openapi/v2"):
            return True
        cluster_bindings = self.store.list(self._crb_gvr)[0]
        rules = self._role_rules_for_bindings(cluster_bindings, user, None)
        if attrs.namespace:
            role_bindings = self.store.list(self._rb_gvr, attrs.namespace)[0]
            rules.extend(self._role_rules_for_bindings(role_bindings, user, attrs.namespace))
        return any(_rule_allows(rule, attrs) for rule in rules)

    def can_i(self, user: User, attrs: RequestAttributes) -> bool:
        return self.authorize(user, attrs)


def bootstrap_rbac(store, registry) -> None:
    """Create the conventional built-in cluster roles and a discovery binding."""
    cluster_role_gvr = ("rbac.authorization.k8s.io", "v1", "clusterroles")
    crb_gvr = ("rbac.authorization.k8s.io", "v1", "clusterrolebindings")

    def ensure(kind: str, gvr, name: str, body: dict) -> None:
        if store.get(gvr, "", name) is None:
            body = {
                "apiVersion": "rbac.authorization.k8s.io/v1",
                "kind": kind,
                "metadata": {"name": name, "labels": {"kubernetes.io/bootstrapping": "rbac-defaults"}},
                **body,
            }
            store.create(gvr, body, kind=kind, namespace="")

    ensure("ClusterRole", cluster_role_gvr, "cluster-admin", {
        "rules": [{"apiGroups": ["*"], "resources": ["*"], "verbs": ["*"], "nonResourceURLs": ["*"]}]
    })
    ensure("ClusterRole", cluster_role_gvr, "admin", {
        "rules": [
            {"apiGroups": ["*"], "resources": ["*"], "verbs": ["*"]},
            {"nonResourceURLs": ["*"], "verbs": ["*"]},
        ]
    })
    ensure("ClusterRole", cluster_role_gvr, "edit", {
        "rules": [{"apiGroups": ["", "apps", "batch", "autoscaling", "networking.k8s.io"],
                   "resources": ["*"], "verbs": ["get", "list", "watch", "create", "update", "patch", "delete"]}]
    })
    ensure("ClusterRole", cluster_role_gvr, "view", {
        "rules": [{"apiGroups": ["", "apps", "batch", "autoscaling", "networking.k8s.io"],
                   "resources": ["*"], "verbs": ["get", "list", "watch"]}]
    })
    ensure("ClusterRole", cluster_role_gvr, "system:discovery", {
        "rules": [{"nonResourceURLs": ["/api", "/api/*", "/apis", "/apis/*", "/version", "/openapi/*", "/healthz", "/livez", "/readyz"],
                   "verbs": ["get"]}]
    })
    ensure("ClusterRole", cluster_role_gvr, "system:basic-user", {
        "rules": [{"apiGroups": ["authorization.k8s.io"], "resources": ["selfsubjectaccessreviews", "selfsubjectrulesreviews"],
                   "verbs": ["create"]}]
    })
    ensure("ClusterRoleBinding", crb_gvr, "system:discovery", {
        "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "ClusterRole", "name": "system:discovery"},
        "subjects": [{"apiGroup": "rbac.authorization.k8s.io", "kind": "Group", "name": "system:authenticated"}],
    })
    ensure("ClusterRoleBinding", crb_gvr, "system:basic-user", {
        "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "ClusterRole", "name": "system:basic-user"},
        "subjects": [{"apiGroup": "rbac.authorization.k8s.io", "kind": "Group", "name": "system:authenticated"}],
    })
    ensure("ClusterRoleBinding", crb_gvr, "mini-k8s:admin", {
        "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "ClusterRole", "name": "cluster-admin"},
        "subjects": [{"apiGroup": "rbac.authorization.k8s.io", "kind": "Group", "name": SYSTEM_MASTERS}],
    })
