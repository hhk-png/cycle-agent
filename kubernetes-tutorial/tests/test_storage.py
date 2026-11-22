from __future__ import annotations

import time


def pvc(name="data", storage="1Gi", storage_class=None, access_modes=None, namespace="default"):
    spec = {
        "accessModes": access_modes or ["ReadWriteOnce"],
        "resources": {"requests": {"storage": storage}},
    }
    if storage_class is not None:
        spec["storageClassName"] = storage_class
    return {"apiVersion": "v1", "kind": "PersistentVolumeClaim",
            "metadata": {"name": name, "namespace": namespace}, "spec": spec}


def consumer(name, claim, namespace="default"):
    return {
        "apiVersion": "v1", "kind": "Pod",
        "metadata": {"name": name, "namespace": namespace},
        "spec": {"containers": [{"name": "c", "image": "x"}],
                 "volumes": [{"name": "data", "persistentVolumeClaim": {"claimName": claim}}]},
    }


def test_dynamic_provisioning_binds_pvc_and_pod(cluster):
    cluster.apply(pvc())
    cluster.wait_until(lambda: (cluster.get("persistentvolumeclaims", "data") or {}).get("status", {}).get("phase") == "Bound",
                       timeout=8, description="pvc bound")
    claim = cluster.get("persistentvolumeclaims", "data")
    assert claim["status"]["capacity"]["storage"] == "1Gi"
    volume = cluster.get("persistentvolumes", claim["spec"]["volumeName"])
    assert volume["spec"]["storageClassName"] == "standard"
    assert volume["spec"]["hostPath"]["path"].endswith("/default/data")
    cluster.apply(consumer("app", "data"))
    cluster.wait_for_pod("app")
    assert cluster.get("pods", "app")["status"]["phase"] == "Running"


def test_static_pv_binding_and_retain(cluster):
    cluster.apply({
        "apiVersion": "v1", "kind": "PersistentVolume",
        "metadata": {"name": "static-pv", "labels": {"tier": "gold"}},
        "spec": {"capacity": {"storage": "5Gi"}, "accessModes": ["ReadWriteOnce"],
                 "storageClassName": "manual", "persistentVolumeReclaimPolicy": "Retain",
                 "hostPath": {"path": "/tmp/static"}},
    })
    claim = pvc("static", storage="4Gi", storage_class="manual")
    claim["spec"]["selector"] = {"matchLabels": {"tier": "gold"}}
    cluster.apply(claim)
    cluster.wait_until(lambda: (cluster.get("persistentvolumeclaims", "static") or {}).get("status", {}).get("phase") == "Bound",
                       timeout=8, description="static binding")
    pv = cluster.get("persistentvolumes", "static-pv")
    assert pv["status"]["phase"] == "Bound"
    assert pv["spec"]["claimRef"]["name"] == "static"
    cluster.delete({"apiVersion": "v1", "kind": "PersistentVolumeClaim",
                    "metadata": {"name": "static", "namespace": "default"}})
    cluster.wait_until(lambda: (cluster.get("persistentvolumes", "static-pv") or {}).get("status", {}).get("phase") in (None, "Released", "Available"),
                       timeout=8, description="pv released")
    pv = cluster.get("persistentvolumes", "static-pv")
    assert pv is not None
    assert pv.get("status", {}).get("phase") == "Released"


def test_wait_for_first_consumer(cluster):
    cluster.apply({"apiVersion": "storage.k8s.io/v1", "kind": "StorageClass",
                   "metadata": {"name": "wffc"},
                   "provisioner": "mini-k8s.io/hostpath", "volumeBindingMode": "WaitForFirstConsumer",
                   "reclaimPolicy": "Delete"})
    cluster.apply(pvc("wffc", storage_class="wffc"))
    time.sleep(0.4)
    claim = cluster.get("persistentvolumeclaims", "wffc")
    assert claim["status"]["phase"] == "Pending"
    assert claim["status"]["reason"] == "WaitingForFirstConsumerToBeCreatedBeforeBinding"
    cluster.apply(consumer("wffc-app", "wffc"))
    cluster.wait_for_pod("wffc-app")
    assert cluster.get("persistentvolumeclaims", "wffc")["status"]["phase"] == "Bound"


def test_pvc_pending_when_no_storage_class(cluster):
    cluster.apply(pvc("unsatisfied", storage_class="does-not-exist"))
    time.sleep(0.3)
    claim = cluster.get("persistentvolumeclaims", "unsatisfied")
    assert claim["status"]["phase"] == "Pending"
    assert claim["status"]["reason"] == "NoPersistentVolumeAvailable"
    cluster.apply(consumer("waiting", "unsatisfied"))
    time.sleep(0.3)
    assert cluster.get("pods", "waiting")["status"]["phase"] == "Pending"


def test_pvc_finalizer_protects_mounted_claim(cluster):
    cluster.apply(pvc("protected"))
    cluster.wait_until(lambda: (cluster.get("persistentvolumeclaims", "protected") or {}).get("status", {}).get("phase") == "Bound",
                       timeout=8, description="pvc bound")
    cluster.apply(consumer("holder", "protected"))
    cluster.wait_for_pod("holder")
    cluster.delete({"apiVersion": "v1", "kind": "PersistentVolumeClaim",
                    "metadata": {"name": "protected", "namespace": "default"}})
    time.sleep(0.4)
    claim = cluster.get("persistentvolumeclaims", "protected")
    assert claim is not None
    assert claim["metadata"].get("deletionTimestamp")


def test_explicit_volume_name_cannot_double_bind(cluster):
    cluster.apply({
        "apiVersion": "v1", "kind": "PersistentVolume",
        "metadata": {"name": "pv-static"},
        "spec": {"capacity": {"storage": "5Gi"}, "accessModes": ["ReadWriteOnce"],
                 "storageClassName": "manual", "persistentVolumeReclaimPolicy": "Retain",
                 "hostPath": {"path": "/tmp/static"}},
    })
    first = pvc("first", storage="1Gi", storage_class="manual")
    first["spec"]["volumeName"] = "pv-static"
    second = pvc("second", storage="1Gi", storage_class="manual")
    second["spec"]["volumeName"] = "pv-static"
    cluster.apply(first)
    cluster.apply(second)
    cluster.wait_until(lambda: (cluster.get("persistentvolumeclaims", "first") or {}).get("status", {}).get("phase") == "Bound",
                       timeout=8, description="first pvc bound")
    time.sleep(0.4)
    assert cluster.get("persistentvolumeclaims", "second")["status"]["phase"] == "Pending"
    assert cluster.get("persistentvolumes", "pv-static")["spec"]["claimRef"]["name"] == "first"


def test_bound_pv_is_protected_until_claim_is_deleted(cluster):
    cluster.apply({
        "apiVersion": "v1", "kind": "PersistentVolume",
        "metadata": {"name": "protected-pv"},
        "spec": {"capacity": {"storage": "5Gi"}, "accessModes": ["ReadWriteOnce"],
                 "storageClassName": "manual", "persistentVolumeReclaimPolicy": "Retain",
                 "hostPath": {"path": "/tmp/protected"}},
    })
    cluster.apply(pvc("claim", storage_class="manual"))
    cluster.wait_until(lambda: (cluster.get("persistentvolumeclaims", "claim") or {}).get("status", {}).get("phase") == "Bound",
                       timeout=8, description="pvc bound")
    cluster.delete({"apiVersion": "v1", "kind": "PersistentVolume",
                    "metadata": {"name": "protected-pv"}})
    time.sleep(0.4)
    pv = cluster.get("persistentvolumes", "protected-pv")
    assert pv is not None and pv["metadata"].get("deletionTimestamp")
    cluster.delete({"apiVersion": "v1", "kind": "PersistentVolumeClaim",
                    "metadata": {"name": "claim", "namespace": "default"}})
    cluster.wait_until(lambda: cluster.get("persistentvolumes", "protected-pv") is None,
                       timeout=8, description="pv released and deleted")


def test_explicit_empty_storage_class_does_not_use_default(cluster):
    cluster.apply(pvc("no-class", storage_class=""))
    time.sleep(0.4)
    claim = cluster.get("persistentvolumeclaims", "no-class")
    assert claim["spec"]["storageClassName"] == ""
    assert claim["status"]["phase"] == "Pending"
