"""Namespace finalization, service accounts, node health, GC and CRD lifecycle."""

from __future__ import annotations

from datetime import datetime, timezone

from mini_k8s.apimachinery.meta import deep_copy, parse_time, remove_finalizer
from mini_k8s.controllers.base import Controller, condition, set_condition

NS_GVR = ("", "v1", "namespaces")
SA_GVR = ("", "v1", "serviceaccounts")
NODE_GVR = ("", "v1", "nodes")
POD_GVR = ("", "v1", "pods")
CRD_GVR = ("apiextensions.k8s.io", "v1", "customresourcedefinitions")


class NamespaceController(Controller):
    name = "namespace-controller"

    def __init__(self, store, registry, *, admission=None, interval: float = 0.1):
        super().__init__(store, interval=interval)
        self.registry = registry
        self.admission = admission

    def reconcile(self) -> None:
        for namespace in self.store.list(NS_GVR)[0]:
            if namespace["metadata"].get("deletionTimestamp"):
                self._finalize(namespace)
            else:
                self._ensure_finalizer(namespace)

    def _ensure_finalizer(self, namespace: dict) -> None:
        if "kubernetes" in namespace["metadata"].get("finalizers", []):
            return
        updated = deep_copy(namespace)
        updated["metadata"].setdefault("finalizers", []).append("kubernetes")
        try:
            self.store.update(NS_GVR, updated, kind="Namespace", namespace="")
        except Exception:
            pass

    def _finalize(self, namespace: dict) -> None:
        name = namespace["metadata"]["name"]
        remaining = 0
        for definition in self.registry.all():
            if not definition.namespaced:
                continue
            try:
                objects = self.store.list(definition.gvr, name)[0]
            except Exception:
                continue
            for obj in objects:
                try:
                    self.store.delete(definition.gvr, name, obj["metadata"]["name"], kind=definition.kind)
                except Exception:
                    pass
                remaining += 1
        if remaining:
            return
        if "kubernetes" in namespace["metadata"].get("finalizers", []):
            updated = deep_copy(namespace)
            remove_finalizer(updated, "kubernetes")
            try:
                self.store.update(NS_GVR, updated, kind="Namespace", namespace="")
            except Exception:
                return
        try:
            self.store.delete(NS_GVR, "", name, kind="Namespace")
        except Exception:
            pass


class ServiceAccountController(Controller):
    name = "serviceaccount-controller"

    def __init__(self, store, *, admission=None, interval: float = 0.1):
        super().__init__(store, interval=interval)
        self.admission = admission

    def reconcile(self) -> None:
        for namespace in self.store.list(NS_GVR)[0]:
            if namespace["metadata"].get("deletionTimestamp"):
                continue
            if namespace["metadata"]["name"] == "kube-system" and not namespace["metadata"].get("uid"):
                continue
            name = "default"
            if self.store.get(SA_GVR, namespace["metadata"]["name"], name) is None:
                body = {
                    "apiVersion": "v1", "kind": "ServiceAccount",
                    "metadata": {"name": name, "namespace": namespace["metadata"]["name"]},
                }
                try:
                    if self.admission:
                        from mini_k8s.admission.admission import AdmissionRequest
                        body = self.admission.admit(AdmissionRequest(
                            "CREATE", SA_GVR, "ServiceAccount", body,
                            namespace=namespace["metadata"]["name"], name=name,
                            username="system:controller:serviceaccount-controller",
                            groups=["system:serviceaccounts", "system:controllers"]))
                    self.store.create(SA_GVR, body, kind="ServiceAccount", namespace=namespace["metadata"]["name"])
                except Exception:
                    pass


class NodeLifecycleController(Controller):
    name = "node-lifecycle-controller"

    def __init__(self, store, *, interval: float = 1.0, grace_seconds: float = 20.0):
        super().__init__(store, interval=interval)
        self.grace_seconds = grace_seconds

    def reconcile(self) -> None:
        now = datetime.now(timezone.utc)
        for node in self.store.list(NODE_GVR)[0]:
            if node["metadata"].get("deletionTimestamp"):
                continue
            conditions = node.get("status", {}).get("conditions", [])
            heartbeat = None
            for cond in conditions:
                if cond.get("type") == "Ready":
                    heartbeat = parse_time(cond.get("lastHeartbeatTime"))
            updated = deep_copy(node)
            ready = any(c.get("type") == "Ready" and c.get("status") == "True" for c in conditions)
            if heartbeat is None:
                heartbeat = parse_time(node.get("metadata", {}).get("creationTimestamp"))
            if heartbeat and (now - heartbeat).total_seconds() > self.grace_seconds and ready:
                for cond in updated.setdefault("status", {}).setdefault("conditions", []):
                    if cond.get("type") == "Ready":
                        cond["status"] = "False"
                        cond["reason"] = "NodeStatusUnknown"
                        cond["message"] = "Kubelet stopped posting node status."
                        cond["lastTransitionTime"] = now.isoformat().replace("+00:00", "Z")
                taints = updated.setdefault("spec", {}).setdefault("taints", [])
                if not any(t.get("key") == "node.kubernetes.io/not-ready" for t in taints):
                    taints.append({"key": "node.kubernetes.io/not-ready", "effect": "NoSchedule",
                                   "timeAdded": now.isoformat().replace("+00:00", "Z")})
                try:
                    self.store.update(NODE_GVR, updated, kind="Node", namespace="")
                except Exception:
                    pass


class GarbageCollector(Controller):
    name = "garbage-collector"

    def __init__(self, store, registry, *, interval: float = 0.2):
        super().__init__(store, interval=interval)
        self.registry = registry

    def reconcile(self) -> None:
        for definition in self.registry.all():
            try:
                objects = self.store.list(definition.gvr)[0]
            except Exception:
                continue
            for obj in objects:
                refs = obj.get("metadata", {}).get("ownerReferences", [])
                if not refs:
                    continue
                owners_alive = False
                for ref in refs:
                    try:
                        owner_def = self.registry.by_kind(ref.get("apiVersion", "v1"), ref.get("kind", ""))
                    except Exception:
                        owners_alive = True
                        continue
                    namespace = "" if not owner_def.namespaced else obj["metadata"].get("namespace", "")
                    owner = self.store.get(owner_def.gvr, namespace, ref.get("name", ""))
                    if owner is not None and owner["metadata"].get("uid") == ref.get("uid", owner["metadata"].get("uid")):
                        owners_alive = True
                        break
                if not owners_alive:
                    try:
                        self.store.delete(definition.gvr, obj["metadata"].get("namespace", ""),
                                          obj["metadata"]["name"], kind=definition.kind)
                    except Exception:
                        pass


class CRDController(Controller):
    name = "crd-controller"

    def __init__(self, store, registry, *, interval: float = 0.2):
        super().__init__(store, interval=interval)
        self.registry = registry

    def reconcile(self) -> None:
        for crd in self.store.list(CRD_GVR)[0]:
            if crd["metadata"].get("deletionTimestamp"):
                self._cleanup(crd)
                continue
            if "customresourcecleanup.apiextensions.k8s.io" not in crd["metadata"].get("finalizers", []):
                updated = deep_copy(crd)
                updated["metadata"].setdefault("finalizers", []).append("customresourcecleanup.apiextensions.k8s.io")
                try:
                    crd = self.store.update(CRD_GVR, updated, kind="CustomResourceDefinition", namespace="")
                except Exception:
                    continue
            self._status(crd)

    def _cleanup(self, crd: dict) -> None:
        try:
            definitions = self.registry.from_crd(crd)
        except Exception:
            definitions = []
        remaining = 0
        for definition in definitions:
            for obj in self.store.list(definition.gvr)[0]:
                try:
                    self.store.delete(definition.gvr, obj["metadata"].get("namespace", ""),
                                      obj["metadata"]["name"], kind=definition.kind)
                except Exception:
                    pass
                remaining += 1
        if remaining:
            return
        updated = deep_copy(crd)
        remove_finalizer(updated, "customresourcecleanup.apiextensions.k8s.io")
        try:
            self.store.update(CRD_GVR, updated, kind="CustomResourceDefinition", namespace="")
            self.store.delete(CRD_GVR, "", crd["metadata"]["name"], kind="CustomResourceDefinition")
        except Exception:
            pass

    def _status(self, crd: dict) -> None:
        status = crd.get("status", {})
        names = crd.get("spec", {}).get("names", {})
        versions = [v["name"] for v in crd.get("spec", {}).get("versions", []) if v.get("served", True)]
        stored = [v["name"] for v in crd.get("spec", {}).get("versions", []) if v.get("storage", False)] or versions[:1]
        conditions = status.get("conditions", [])
        conditions = set_condition(conditions, condition("NamesAccepted", True, "NoConflicts", "no conflicts found"))
        conditions = set_condition(conditions, condition("Established", True, "InitialNamesAccepted",
                                                         "the initial names have been accepted"))
        desired = {
            "conditions": conditions,
            "acceptedNames": {"plural": names.get("plural", ""), "singular": names.get("singular", ""),
                              "kind": names.get("kind", ""), "shortNames": names.get("shortNames", [])},
            "storedVersions": stored,
        }
        if status != desired:
            self.update_status(CRD_GVR, crd, desired)
