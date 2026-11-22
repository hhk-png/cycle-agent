"""PersistentVolume binding and simple dynamic provisioning."""

from __future__ import annotations

import uuid

from mini_k8s.apimachinery.meta import deep_copy, now_iso
from mini_k8s.apimachinery.quantity import parse_quantity
from mini_k8s.apimachinery.selectors import match_expression
from mini_k8s.controllers.base import Controller

PV_GVR = ("", "v1", "persistentvolumes")
PVC_GVR = ("", "v1", "persistentvolumeclaims")
SC_GVR = ("storage.k8s.io", "v1", "storageclasses")
POD_GVR = ("", "v1", "pods")


class PVController(Controller):
    name = "persistentvolume-controller"

    def __init__(self, store, *, interval: float = 0.05, provision_root: str = "/tmp/mini-k8s-volumes"):
        super().__init__(store, interval=interval)
        self.provision_root = provision_root

    def reconcile(self) -> None:
        self._ensure_finalizers_and_default_class()
        pvs = self.store.list(PV_GVR)[0]
        for pv in pvs:
            if pv["metadata"].get("deletionTimestamp"):
                self._finalize_pv(pv)
        pvs = self.store.list(PV_GVR)[0]
        pvcs = self.store.list(PVC_GVR)[0]
        for pvc in pvcs:
            if pvc["metadata"].get("deletionTimestamp"):
                self._finalize_pvc(pvc)
                continue
            if pvc.get("status", {}).get("phase") == "Bound":
                continue
            if pvc.get("spec", {}).get("volumeName"):
                pv = self.store.get(PV_GVR, "", pvc["spec"]["volumeName"])
                if pv and self._pv_matches(pvc, pv):
                    self._bind(pvc, pv)
                else:
                    self._set_pending(pvc, "VolumeMismatch")
                continue
            spec = pvc.get("spec", {})
            # None means "unset → apply the default StorageClass"; an explicit
            # empty string means "no class, static binding only".
            if "storageClassName" not in spec:
                default_name = self._default_storage_class()
                if default_name:
                    updated_pvc = deep_copy(pvc)
                    updated_pvc["spec"]["storageClassName"] = default_name
                    try:
                        pvc = self.store.update(PVC_GVR, updated_pvc, kind="PersistentVolumeClaim",
                                                namespace=pvc["metadata"]["namespace"])
                    except Exception:
                        pass
            sc_name = pvc.get("spec", {}).get("storageClassName")
            sc = self.store.get(SC_GVR, "", sc_name) if sc_name else None
            if sc and sc.get("volumeBindingMode") == "WaitForFirstConsumer":
                if not self._consumer_scheduled(pvc):
                    self._set_pending(pvc, "WaitingForFirstConsumerToBeCreatedBeforeBinding")
                    continue
            match = self._find_pv(pvc, pvs)
            if match:
                self._bind(pvc, match)
                continue
            if sc:
                pv = self._provision(pvc, sc)
                if pv:
                    pvs.append(pv)
                    self._bind(pvc, pv)
                    continue
            self._set_pending(pvc, "NoPersistentVolumeAvailable")

    def _default_storage_class(self) -> str | None:
        for sc in self.store.list(SC_GVR)[0]:
            if sc.get("metadata", {}).get("annotations", {}).get("storageclass.kubernetes.io/is-default-class") == "true":
                return sc["metadata"]["name"]
        return None

    def _ensure_finalizers_and_default_class(self) -> None:
        for pvc in self.store.list(PVC_GVR)[0]:
            if "kubernetes.io/pvc-protection" not in pvc["metadata"].get("finalizers", []):
                updated = deep_copy(pvc)
                updated["metadata"].setdefault("finalizers", []).append("kubernetes.io/pvc-protection")
                try:
                    self.store.update(PVC_GVR, updated, kind="PersistentVolumeClaim", namespace=pvc["metadata"]["namespace"])
                except Exception:
                    pass
        for pv in self.store.list(PV_GVR)[0]:
            if pv.get("spec", {}).get("claimRef") and \
                    "kubernetes.io/pv-protection" not in pv["metadata"].get("finalizers", []):
                updated = deep_copy(pv)
                updated["metadata"].setdefault("finalizers", []).append("kubernetes.io/pv-protection")
                try:
                    self.store.update(PV_GVR, updated, kind="PersistentVolume", namespace="")
                except Exception:
                    pass

    def _consumer_scheduled(self, pvc: dict) -> bool:
        namespace = pvc["metadata"]["namespace"]
        for pod in self.store.list(POD_GVR, namespace)[0]:
            for volume in pod.get("spec", {}).get("volumes", []):
                if volume.get("persistentVolumeClaim", {}).get("claimName") == pvc["metadata"]["name"]:
                    if pod.get("spec", {}).get("nodeName"):
                        return True
        return False

    def _find_pv(self, pvc: dict, pvs: list[dict]) -> dict | None:
        for pv in pvs:
            if self._pv_matches(pvc, pv):
                return pv
        return None

    def _pv_matches(self, pvc: dict, pv: dict) -> bool:
        """Check whether a specific PV can satisfy a PVC.

        Used both for static matching and for an explicit ``volumeName``;
        without these checks two PVCs could bind the same PV.
        """
        if pv.get("status", {}).get("phase") not in (None, "Available", ""):
            return False
        claim = pv.get("spec", {}).get("claimRef")
        if claim and not (claim.get("namespace") == pvc["metadata"].get("namespace")
                          and claim.get("name") == pvc["metadata"].get("name")):
            return False
        spec = pvc.get("spec", {})
        pv_spec = pv.get("spec", {})
        requested = parse_quantity(spec.get("resources", {}).get("requests", {}).get("storage", "0"))
        capacity = parse_quantity(pv_spec.get("capacity", {}).get("storage", "0"))
        if capacity < requested:
            return False
        modes = set(spec.get("accessModes", []))
        if modes and not modes.issubset(set(pv_spec.get("accessModes", []))):
            return False
        if spec.get("volumeMode", "Filesystem") != pv_spec.get("volumeMode", "Filesystem"):
            return False
        if "storageClassName" in spec and pv_spec.get("storageClassName") != spec["storageClassName"]:
            return False
        selector = spec.get("selector", {})
        labels = pv["metadata"].get("labels", {})
        if selector.get("matchLabels") and not all(
                labels.get(k) == v for k, v in selector["matchLabels"].items()):
            return False
        for expression in selector.get("matchExpressions", []):
            if not match_expression(expression, labels):
                return False
        return True

    def _bind(self, pvc: dict, pv: dict) -> None:
        namespace = pvc["metadata"]["namespace"]
        claim = pv.get("spec", {}).get("claimRef")
        if claim and (claim.get("namespace"), claim.get("name")) != (namespace, pvc["metadata"]["name"]):
            return
        now = now_iso()
        pvc_copy = deep_copy(pvc)
        pvc_copy["spec"]["volumeName"] = pv["metadata"]["name"]
        pvc_copy["status"] = {
            "phase": "Bound",
            "accessModes": pv.get("spec", {}).get("accessModes", pvc.get("spec", {}).get("accessModes", [])),
            "capacity": pv.get("spec", {}).get("capacity", {}),
        }
        pv_copy = deep_copy(pv)
        pv_copy["spec"]["claimRef"] = {"kind": "PersistentVolumeClaim", "name": pvc["metadata"]["name"],
                                       "namespace": namespace, "uid": pvc["metadata"].get("uid"),
                                       "apiVersion": "v1", "resourceVersion": pvc["metadata"].get("resourceVersion")}
        # Install protection at bind time so an immediate DELETE cannot race
        # the next reconcile loop and destroy a bound volume.
        if "kubernetes.io/pv-protection" not in pv_copy["metadata"].get("finalizers", []):
            pv_copy["metadata"].setdefault("finalizers", []).append("kubernetes.io/pv-protection")
        pv_copy["status"] = {"phase": "Bound"}
        pv_copy.setdefault("metadata", {}).setdefault("annotations", {})["pv.kubernetes.io/bound-by-controller"] = "yes"
        pvc_copy.setdefault("metadata", {}).setdefault("annotations", {})["pv.kubernetes.io/bind-completed"] = now
        try:
            self.store.update(PV_GVR, pv_copy, kind="PersistentVolume", namespace="")
            self.store.update(PVC_GVR, pvc_copy, kind="PersistentVolumeClaim", namespace=namespace)
            self.event("Normal", "Bound", f"Bound PVC {namespace}/{pvc['metadata']['name']} to PV {pv['metadata']['name']}", pvc)
        except Exception:
            pass

    def _provision(self, pvc: dict, sc: dict) -> dict | None:
        if not sc.get("provisioner"):
            return None
        namespace = pvc["metadata"]["namespace"]
        name = pvc["metadata"]["name"]
        pv_name = f"pvc-{pvc['metadata'].get('uid', uuid.uuid4())}"
        request = pvc.get("spec", {}).get("resources", {}).get("requests", {}).get("storage", "1Gi")
        reclaim = sc.get("reclaimPolicy", "Delete")
        volume_mode = pvc.get("spec", {}).get("volumeMode", "Filesystem")
        pv = {
            "apiVersion": "v1", "kind": "PersistentVolume",
            "metadata": {
                "name": pv_name,
                "labels": {"topology.kubernetes.io/region": "mini", "topology.kubernetes.io/zone": "mini-a"},
                "annotations": {"pv.kubernetes.io/provisioned-by": sc.get("provisioner", "mini-k8s.io/hostpath")},
            },
            "spec": {
                "capacity": {"storage": request},
                "accessModes": pvc.get("spec", {}).get("accessModes", ["ReadWriteOnce"]),
                "persistentVolumeReclaimPolicy": reclaim,
                "storageClassName": sc["metadata"]["name"],
                "volumeMode": volume_mode,
                "hostPath": {"path": f"{self.provision_root}/{namespace}/{name}", "type": "DirectoryOrCreate"},
            },
            "status": {"phase": "Available"},
        }
        if pvc.get("spec", {}).get("volumeAttributesClassName"):
            pv["spec"]["volumeAttributesClassName"] = pvc["spec"]["volumeAttributesClassName"]
        try:
            return self.store.create(PV_GVR, pv, kind="PersistentVolume", namespace="")
        except Exception:
            return None

    def _set_pending(self, pvc: dict, reason: str) -> None:
        if pvc.get("status", {}).get("phase") == "Pending" and pvc.get("status", {}).get("reason") == reason:
            return
        status = {"phase": "Pending", "reason": reason,
                  "accessModes": pvc.get("spec", {}).get("accessModes", [])}
        self.update_status(PVC_GVR, pvc, status)

    def _finalize_pvc(self, pvc: dict) -> None:
        namespace = pvc["metadata"]["namespace"]
        in_use = False
        for pod in self.store.list(POD_GVR, namespace)[0]:
            for volume in pod.get("spec", {}).get("volumes", []):
                if volume.get("persistentVolumeClaim", {}).get("claimName") == pvc["metadata"]["name"]:
                    in_use = True
        if in_use:
            return
        pvc_copy = deep_copy(pvc)
        pvc_copy["metadata"]["finalizers"] = [f for f in pvc_copy["metadata"].get("finalizers", [])
                                              if f != "kubernetes.io/pvc-protection"]
        try:
            self.store.update(PVC_GVR, pvc_copy, kind="PersistentVolumeClaim", namespace=namespace)
        except Exception:
            return
        pv_name = pvc.get("spec", {}).get("volumeName")
        if pv_name:
            pv = self.store.get(PV_GVR, "", pv_name)
            if pv:
                policy = pv.get("spec", {}).get("persistentVolumeReclaimPolicy", "Retain")
                if policy == "Delete":
                    try:
                        self.store.delete(PV_GVR, "", pv_name, kind="PersistentVolume")
                    except Exception:
                        pass
                else:
                    pv_copy = deep_copy(pv)
                    pv_copy["spec"].pop("claimRef", None)
                    pv_copy["status"] = {"phase": "Released"}
                    try:
                        self.store.update(PV_GVR, pv_copy, kind="PersistentVolume", namespace="")
                    except Exception:
                        pass

    def _finalize_pv(self, pv: dict) -> None:
        """Remove pv-protection once the bound claim is gone."""
        claim = pv.get("spec", {}).get("claimRef")
        if claim:
            pvc = self.store.get(PVC_GVR, claim.get("namespace", "default"), claim.get("name", ""))
            if pvc is not None:
                return
        if "kubernetes.io/pv-protection" not in pv["metadata"].get("finalizers", []):
            return
        updated = deep_copy(pv)
        updated["metadata"]["finalizers"] = [
            f for f in updated["metadata"].get("finalizers", []) if f != "kubernetes.io/pv-protection"
        ]
        try:
            # update() completes the deletion when the last finalizer is gone.
            self.store.update(PV_GVR, updated, kind="PersistentVolume", namespace="")
        except Exception:
            pass
