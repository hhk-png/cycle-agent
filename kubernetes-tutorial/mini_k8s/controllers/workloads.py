"""Deployment, ReplicaSet, StatefulSet, DaemonSet, Job and CronJob controllers."""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from mini_k8s.apimachinery.cron import parse_cron
from mini_k8s.apimachinery.meta import (
    deep_copy, now_iso, owner_reference, parse_time, stable_hash,
)
from mini_k8s.apimachinery.selectors import match_expression
from mini_k8s.controllers.base import (
    Controller, condition, pod_template_hash, resolve_int_or_percent, set_condition,
)

POD_GVR = ("", "v1", "pods")
DEPLOYMENT_GVR = ("apps", "v1", "deployments")
RS_GVR = ("apps", "v1", "replicasets")
STS_GVR = ("apps", "v1", "statefulsets")
DS_GVR = ("apps", "v1", "daemonsets")
JOB_GVR = ("batch", "v1", "jobs")
CRONJOB_GVR = ("batch", "v1", "cronjobs")
PVC_GVR = ("", "v1", "persistentvolumeclaims")
NODE_GVR = ("", "v1", "nodes")

# DaemonSet pods tolerate the taints that kubelet/controller-manager add for
# node lifecycle and pressure.  They also stay on cordoned nodes.
DS_DEFAULT_TOLERATIONS = [
    {"key": "node.kubernetes.io/not-ready", "operator": "Exists", "effect": "NoExecute"},
    {"key": "node.kubernetes.io/unreachable", "operator": "Exists", "effect": "NoExecute"},
    {"key": "node.kubernetes.io/disk-pressure", "operator": "Exists", "effect": "NoSchedule"},
    {"key": "node.kubernetes.io/memory-pressure", "operator": "Exists", "effect": "NoSchedule"},
    {"key": "node.kubernetes.io/pid-pressure", "operator": "Exists", "effect": "NoSchedule"},
    {"key": "node.kubernetes.io/unschedulable", "operator": "Exists", "effect": "NoSchedule"},
]


def apply_pod_defaults(pod: dict) -> dict:
    """Apply the API server defaults controllers rely on when writing Pods.

    Controllers in real Kubernetes submit Pods to the API server, so admission
    runs for them too.  MiniK8s' controllers write to the store directly; this
    helper keeps the resulting Pods shaped like API-created Pods.
    """
    pod.setdefault("status", {"phase": "Pending"})
    spec = pod.setdefault("spec", {})
    spec.setdefault("restartPolicy", "Always")
    spec.setdefault("dnsPolicy", "ClusterFirst")
    spec.setdefault("terminationGracePeriodSeconds", 30)
    spec.setdefault("serviceAccountName", "default")
    spec.setdefault("schedulerName", "mini-scheduler")
    spec.setdefault("enableServiceLinks", True)
    spec.setdefault("priority", 0)
    for container in spec.get("initContainers", []) + spec.get("containers", []):
        container.setdefault("imagePullPolicy", "IfNotPresent")
        container.setdefault("terminationMessagePath", "/dev/termination-log")
        container.setdefault("resources", {})
    return pod


def _node_affinity_ok(template_spec: dict, node: dict) -> bool:
    required = (template_spec.get("affinity", {}).get("nodeAffinity", {})
                .get("requiredDuringSchedulingIgnoredDuringExecution", {})
                .get("nodeSelectorTerms", []))
    if not required:
        return True
    labels = node.get("metadata", {}).get("labels", {})
    name = node["metadata"]["name"]
    for term in required:
        expressions = term.get("matchExpressions", [])
        fields = term.get("matchFields", [])
        if not all(match_expression(expr, labels) for expr in expressions):
            continue
        if all(_match_node_field(expr, name) for expr in fields):
            return True
    return False


def _match_node_field(expression: dict, name: str) -> bool:
    if expression.get("key") != "metadata.name":
        return False
    operator = expression.get("operator", "In")
    values = expression.get("values", [])
    if operator == "In":
        return name in values
    if operator == "NotIn":
        return name not in values
    if operator == "Exists":
        return True
    if operator == "DoesNotExist":
        return False
    return False


def _tolerates(tolerations: list[dict], taint: dict) -> bool:
    for toleration in tolerations:
        effect = toleration.get("effect")
        if effect and effect != taint.get("effect"):
            continue
        operator = toleration.get("operator", "Equal")
        if operator == "Exists":
            if toleration.get("key") in (None, "", taint.get("key")):
                return True
        elif (toleration.get("key") == taint.get("key")
              and toleration.get("value", "") == taint.get("value", "")):
            return True
    return False


def make_pod_from_template(template: dict, *, name: str, namespace: str, owner: dict,
                           labels: dict[str, str] | None = None, annotations: dict[str, str] | None = None,
                           node_name: str | None = None) -> dict:
    template = deep_copy(template)
    meta = template.setdefault("metadata", {})
    pod_labels = {**meta.get("labels", {}), **(labels or {})}
    pod_annotations = {**meta.get("annotations", {}), **(annotations or {})}
    pod = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": pod_labels,
            "annotations": pod_annotations,
            "ownerReferences": [owner_reference(owner)],
        },
        "spec": template.get("spec", {}),
    }
    if node_name:
        pod["spec"]["nodeName"] = node_name
    return apply_pod_defaults(pod)


def _ready(pod: dict) -> bool:
    return any(c.get("type") == "Ready" and c.get("status") == "True" for c in pod.get("status", {}).get("conditions", []))


def _terminal(pod: dict) -> bool:
    return pod.get("status", {}).get("phase") in ("Succeeded", "Failed")


def _pod_available(pod: dict, min_ready_seconds: int) -> bool:
    if not _ready(pod):
        return False
    if min_ready_seconds <= 0:
        return True
    for cond in pod.get("status", {}).get("conditions", []):
        if cond.get("type") == "Ready" and cond.get("status") == "True":
            changed = parse_time(cond.get("lastTransitionTime"))
            return changed is not None and (datetime.now(timezone.utc) - changed).total_seconds() >= min_ready_seconds
    return False


def _controlled_by(pod: dict, owner_uid: str) -> bool:
    return any(ref.get("uid") == owner_uid and ref.get("controller") for ref in pod.get("metadata", {}).get("ownerReferences", []))


def _matches_selector(selector: dict, labels: dict[str, str]) -> bool:
    if not selector:
        return False
    if selector.get("matchLabels") and not all(labels.get(k) == v for k, v in selector["matchLabels"].items()):
        return False
    for expression in selector.get("matchExpressions", []):
        if not match_expression(expression, labels):
            return False
    return bool(selector.get("matchLabels") or selector.get("matchExpressions"))


def _delete_pod(store, pod: dict) -> None:
    try:
        store.delete(POD_GVR, pod["metadata"]["namespace"], pod["metadata"]["name"], kind="Pod")
    except Exception:
        pass


class ReplicaSetController(Controller):
    name = "replicaset-controller"

    def __init__(self, store, *, admission=None, interval: float = 0.05):
        super().__init__(store, interval=interval)
        self.admission = admission

    def reconcile(self) -> None:
        replicasets = self.store.list(RS_GVR)[0]
        pods = self.store.list(POD_GVR)[0]
        for rs in replicasets:
            if rs["metadata"].get("deletionTimestamp"):
                continue
            self._reconcile_rs(rs, pods)

    def _reconcile_rs(self, rs: dict, all_pods: list[dict]) -> None:
        namespace = rs["metadata"]["namespace"]
        uid = rs["metadata"]["uid"]
        selector = rs.get("spec", {}).get("selector", {})
        owned = [p for p in all_pods if p["metadata"].get("namespace") == namespace and _controlled_by(p, uid)]
        # Adopt matching orphans, as the real ReplicaSet controller does.
        for pod in all_pods:
            if pod["metadata"].get("namespace") != namespace or pod["metadata"].get("deletionTimestamp"):
                continue
            if any(ref.get("controller") for ref in pod["metadata"].get("ownerReferences", [])):
                continue
            if _matches_selector(selector, pod["metadata"].get("labels", {})):
                adopted = deep_copy(pod)
                adopted["metadata"].setdefault("ownerReferences", []).append(owner_reference(rs))
                try:
                    pod = self.store.update(POD_GVR, adopted, kind="Pod", namespace=namespace)
                    owned.append(pod)
                except Exception:
                    pass
        desired = int(rs.get("spec", {}).get("replicas", 0))
        active = [p for p in owned if not _terminal(p) and not p["metadata"].get("deletionTimestamp")]
        if len(active) < desired:
            template = rs.get("spec", {}).get("template", {})
            for _ in range(desired - len(active)):
                name = f"{rs['metadata']['name']}-{uuid.uuid4().hex[:5]}"
                pod = make_pod_from_template(
                    template, name=name, namespace=namespace, owner=rs,
                    labels={"pod-template-hash": rs["metadata"].get("labels", {}).get("pod-template-hash", "")},
                )
                if self.admission:
                    from mini_k8s.admission.admission import AdmissionRequest
                    try:
                        pod = self.admission.admit(AdmissionRequest(
                            "CREATE", POD_GVR, "Pod", pod, namespace=namespace, name=name,
                            username="system:controller:replicaset-controller",
                            groups=["system:serviceaccounts", "system:controllers"],
                        ))
                    except Exception as exc:
                        self.event("Warning", "FailedCreate", f"Error creating pod: {exc}", rs)
                        break
                try:
                    created = self.store.create(POD_GVR, pod, kind="Pod", namespace=namespace)
                    owned.append(created)
                    active.append(created)
                except Exception as exc:
                    self.event("Warning", "FailedCreate", f"Error creating pod: {exc}", rs)
                    break
        elif len(active) > desired:
            # Match the real controller's preference: unscheduled before
            # scheduled, pending/unknown before running, not-ready before
            # ready, then the oldest pod first.
            def deletion_rank(pod: dict):
                phase = pod.get("status", {}).get("phase")
                phase_rank = {"Pending": 0, "Unknown": 1, "Running": 2}.get(phase, 3)
                return (
                    bool(pod.get("spec", {}).get("nodeName")),
                    phase_rank,
                    _ready(pod),
                    pod["metadata"].get("creationTimestamp", ""),
                    pod["metadata"]["name"],
                )

            victims = sorted(active, key=deletion_rank)
            for pod in victims[: len(active) - desired]:
                _delete_pod(self.store, pod)
        # Compute status from a fresh list so deletions/creations are reflected.
        pods = self.store.list(POD_GVR, namespace)[0]
        owned = [p for p in pods if _controlled_by(p, uid) and not p["metadata"].get("deletionTimestamp")]
        template_labels = rs.get("spec", {}).get("template", {}).get("metadata", {}).get("labels", {})
        min_ready = int(rs.get("spec", {}).get("minReadySeconds", 0))
        ready = [p for p in owned if _ready(p) and not _terminal(p)]
        available = [p for p in owned if _pod_available(p, min_ready) and not _terminal(p)]
        fully_labeled = [p for p in owned if all(p["metadata"].get("labels", {}).get(k) == v for k, v in template_labels.items())]
        status = {
            "replicas": len(owned),
            "fullyLabeledReplicas": len(fully_labeled),
            "readyReplicas": len(ready),
            "availableReplicas": len(available),
            "observedGeneration": rs["metadata"].get("generation", 1),
        }
        self.update_status(RS_GVR, rs, status)


class DeploymentController(Controller):
    name = "deployment-controller"

    def __init__(self, store, *, admission=None, interval: float = 0.05):
        super().__init__(store, interval=interval)
        self.admission = admission

    def reconcile(self) -> None:
        for deployment in self.store.list(DEPLOYMENT_GVR)[0]:
            if deployment["metadata"].get("deletionTimestamp"):
                continue
            try:
                self._reconcile_deployment(deployment)
            except Exception as exc:
                self.event("Warning", "ReconcileError", str(exc), deployment)

    def _reconcile_deployment(self, deployment: dict) -> None:
        namespace = deployment["metadata"]["namespace"]
        name = deployment["metadata"]["name"]
        spec = deployment.get("spec", {})
        template = deep_copy(spec.get("template", {}))
        template.setdefault("metadata", {}).setdefault("labels", {})
        template_labels = template["metadata"]["labels"]
        if not template_labels:
            template_labels = {"app": name}
            template["metadata"]["labels"] = template_labels
        hash_value = pod_template_hash(template)
        annotations = deployment["metadata"].setdefault("annotations", {})
        previous_hash = annotations.get("mini-k8s.io/last-template-hash")
        revision = int(annotations.get("deployment.kubernetes.io/revision", "0") or 0)
        if previous_hash != hash_value:
            revision = max(revision, self._max_revision(namespace, name)) + (1 if revision else 1)
            annotations["deployment.kubernetes.io/revision"] = str(revision)
            annotations["mini-k8s.io/last-template-hash"] = hash_value
            deployment = self.store.update(DEPLOYMENT_GVR, deployment, kind="Deployment", namespace=namespace)
        elif revision == 0:
            revision = 1
            annotations["deployment.kubernetes.io/revision"] = "1"
            deployment = self.store.update(DEPLOYMENT_GVR, deployment, kind="Deployment", namespace=namespace)
        template["metadata"]["labels"] = {**template_labels, "pod-template-hash": hash_value}
        template["metadata"].setdefault("annotations", {})["pod-template-hash"] = hash_value

        replicasets = [rs for rs in self.store.list(RS_GVR, namespace)[0]
                       if any(ref.get("uid") == deployment["metadata"]["uid"] for ref in rs["metadata"].get("ownerReferences", []))]
        rs_name = f"{name}-{hash_value}"
        new_rs = next((rs for rs in replicasets if rs["metadata"]["name"] == rs_name), None)
        if new_rs is None:
            body = {
                "apiVersion": "apps/v1", "kind": "ReplicaSet",
                "metadata": {
                    "name": rs_name, "namespace": namespace,
                    "labels": {**template_labels, "pod-template-hash": hash_value},
                    "annotations": {
                        "deployment.kubernetes.io/revision": str(revision),
                        "deployment.kubernetes.io/desired-replicas": str(spec.get("replicas", 1)),
                        "deployment.kubernetes.io/max-replicas": str(spec.get("replicas", 1)),
                    },
                    "ownerReferences": [owner_reference(deployment)],
                },
                "spec": {
                    "replicas": 0,
                    "selector": {"matchLabels": {**template_labels, "pod-template-hash": hash_value}},
                    "template": template,
                    "minReadySeconds": spec.get("minReadySeconds", 0),
                },
            }
            try:
                new_rs = self.store.create(RS_GVR, body, kind="ReplicaSet", namespace=namespace)
            except Exception as exc:
                self.event("Warning", "FailedCreate", f"Error creating ReplicaSet: {exc}", deployment)
                return
            replicasets.append(new_rs)
        else:
            # Keep the ReplicaSet template in sync with the Deployment.
            desired_rs = deep_copy(new_rs)
            desired_rs["spec"]["template"] = template
            desired_rs["spec"]["selector"] = {"matchLabels": {**template_labels, "pod-template-hash": hash_value}}
            desired_rs["spec"]["minReadySeconds"] = spec.get("minReadySeconds", 0)
            if desired_rs.get("spec") != new_rs.get("spec"):
                try:
                    new_rs = self.store.update(RS_GVR, desired_rs, kind="ReplicaSet", namespace=namespace)
                    replicasets = [rs for rs in replicasets if rs["metadata"]["name"] != rs_name] + [new_rs]
                except Exception:
                    pass

        desired = int(spec.get("replicas", 1))
        paused = bool(spec.get("paused"))
        strategy = spec.get("strategy", {"type": "RollingUpdate"})
        if paused:
            self._update_status(deployment, replicasets, new_rs, desired)
            return
        if strategy.get("type") == "Recreate":
            old_rs = [rs for rs in replicasets if rs["metadata"]["name"] != rs_name]
            for rs in old_rs:
                if int(rs.get("spec", {}).get("replicas", 0)) != 0:
                    self._scale_rs(rs, 0)
            # Recreate means all old pods are gone before the new template
            # starts.  Terminating pods with finalizers still count as present.
            old_uids = {rs["metadata"]["uid"] for rs in old_rs}
            old_pods = [
                p for p in self.store.list(POD_GVR, namespace)[0]
                if any(ref.get("uid") in old_uids for ref in p["metadata"].get("ownerReferences", []))
            ]
            if old_pods:
                self._update_status(deployment, replicasets, new_rs, desired)
                return
            self._scale_rs(new_rs, desired)
        else:
            rolling = strategy.get("rollingUpdate", {})
            max_surge = resolve_int_or_percent(rolling.get("maxSurge", "25%"), desired)
            # Kubernetes rounds maxSurge up and maxUnavailable down.
            max_unavailable = resolve_int_or_percent(rolling.get("maxUnavailable", "25%"), desired,
                                                      round_up=False)
            if max_surge == 0 and max_unavailable == 0:
                max_unavailable = 1
            self._rollout(deployment, new_rs, replicasets, desired, max_surge, max_unavailable)

        replicasets = [rs for rs in self.store.list(RS_GVR, namespace)[0]
                       if any(ref.get("uid") == deployment["metadata"]["uid"] for ref in rs["metadata"].get("ownerReferences", []))]
        new_rs = next((rs for rs in replicasets if rs["metadata"]["name"] == rs_name), new_rs)
        self._update_status(deployment, replicasets, new_rs, desired)
        self._cleanup_history(deployment, replicasets, new_rs)

    def _rollout(self, deployment: dict, new_rs: dict, replicasets: list[dict], desired: int,
                 max_surge: int, max_unavailable: int) -> None:
        new_rs = self.store.get(RS_GVR, new_rs["metadata"]["namespace"], new_rs["metadata"]["name"]) or new_rs
        new_target = int(new_rs.get("spec", {}).get("replicas", 0))
        new_ready = min(new_target, int(new_rs.get("status", {}).get("availableReplicas", 0)))
        old = [rs for rs in replicasets if rs["metadata"]["uid"] != new_rs["metadata"]["uid"]]
        old_total = sum(int(rs.get("spec", {}).get("replicas", 0)) for rs in old)
        old_available = sum(int(rs.get("status", {}).get("availableReplicas", 0)) for rs in old)

        # How far can old replicas fall without violating maxUnavailable?
        old_floor = max(0, desired - max_unavailable - new_ready)
        old_target = min(old_total, old_floor)
        if old_available < old_target:
            old_target = old_available
        # New replicas may fill any room under desired+maxSurge.
        total_cap = desired + max_surge
        new_target = max(new_target, total_cap - old_target)
        new_target = min(desired, max(new_target, 0))
        if new_target == desired:
            old_target = max(0, min(old_target, total_cap - new_target))
        if old_total != old_target:
            self._scale_old(old, old_target)
        if new_target != int(new_rs.get("spec", {}).get("replicas", 0)):
            self._scale_rs(new_rs, new_target)

    def _scale_old(self, replicasets: list[dict], target: int) -> None:
        if not replicasets:
            return
        # Scale down the oldest first, proportionally.
        ordered = sorted(replicasets, key=lambda rs: int(rs["metadata"].get("annotations", {}).get("deployment.kubernetes.io/revision", "0")))
        remaining = target
        for index, rs in enumerate(ordered):
            current = int(rs.get("spec", {}).get("replicas", 0))
            if index == len(ordered) - 1:
                desired = remaining
            else:
                desired = min(current, remaining)
            remaining -= desired
            if current != desired:
                self._scale_rs(rs, desired)

    def _scale_rs(self, rs: dict, replicas: int) -> None:
        if int(rs.get("spec", {}).get("replicas", 0)) == replicas:
            return
        updated = deep_copy(rs)
        updated["spec"]["replicas"] = replicas
        try:
            self.store.update(RS_GVR, updated, kind="ReplicaSet", namespace=rs["metadata"]["namespace"])
        except Exception:
            pass

    def _max_revision(self, namespace: str, deployment_name: str) -> int:
        values = [0]
        for rs in self.store.list(RS_GVR, namespace)[0]:
            if rs["metadata"]["name"].startswith(deployment_name + "-"):
                values.append(int(rs["metadata"].get("annotations", {}).get("deployment.kubernetes.io/revision", "0") or 0))
        return max(values)

    def _update_status(self, deployment: dict, replicasets: list[dict], new_rs: dict | None, desired: int) -> None:
        replicas = sum(int(rs.get("status", {}).get("replicas", 0)) for rs in replicasets)
        ready = sum(int(rs.get("status", {}).get("readyReplicas", 0)) for rs in replicasets)
        available = sum(int(rs.get("status", {}).get("availableReplicas", 0)) for rs in replicasets)
        updated = int(new_rs.get("status", {}).get("replicas", 0)) if new_rs else 0
        strategy = deployment.get("spec", {}).get("strategy", {})
        max_unavailable = 0
        if strategy.get("type") != "Recreate":
            max_unavailable = resolve_int_or_percent(strategy.get("rollingUpdate", {}).get("maxUnavailable", "25%"),
                                                     desired, round_up=False)
        min_available = max(0, desired - max_unavailable)
        generation = deployment["metadata"].get("generation", 1)
        conditions = deployment.get("status", {}).get("conditions", [])
        available_cond = condition("Available", available >= min_available,
                                   "MinimumReplicasAvailable" if available >= min_available else "MinimumReplicasUnavailable",
                                   f"Deployment has {available} available replicas (minimum {min_available})",
                                   observed_generation=generation)
        conditions = set_condition(conditions, available_cond)
        complete = updated == desired and replicas == desired and available >= desired
        progressing = condition("Progressing", not complete,
                                "NewReplicaSetAvailable" if complete and desired > 0 else "ReplicaSetUpdated",
                                "Deployment has successfully progressed" if complete else f"Replicas updated: {updated}/{desired}",
                                observed_generation=generation)
        # Preserve a terminal Failed condition if a rollout has timed out.
        if not any(c.get("type") == "Progressing" and c.get("status") == "False" and c.get("reason") == "ProgressDeadlineExceeded" for c in conditions):
            conditions = set_condition(conditions, progressing)
        status = {
            "observedGeneration": generation,
            "replicas": replicas,
            "updatedReplicas": updated,
            "readyReplicas": ready,
            "availableReplicas": available,
            "unavailableReplicas": max(0, desired - available),
            "conditions": conditions,
        }
        self.update_status(DEPLOYMENT_GVR, deployment, status)

    def _cleanup_history(self, deployment: dict, replicasets: list[dict], new_rs: dict | None) -> None:
        limit = int(deployment.get("spec", {}).get("revisionHistoryLimit", 10))
        old = sorted(
            [rs for rs in replicasets if not new_rs or rs["metadata"]["uid"] != new_rs["metadata"]["uid"]],
            key=lambda rs: int(rs["metadata"].get("annotations", {}).get("deployment.kubernetes.io/revision", "0")),
            reverse=True,
        )
        for rs in old[limit:]:
            if int(rs.get("spec", {}).get("replicas", 0)) == 0:
                try:
                    self.store.delete(RS_GVR, rs["metadata"]["namespace"], rs["metadata"]["name"], kind="ReplicaSet")
                except Exception:
                    pass


class StatefulSetController(Controller):
    name = "statefulset-controller"

    def __init__(self, store, *, admission=None, interval: float = 0.05):
        super().__init__(store, interval=interval)
        self.admission = admission

    def reconcile(self) -> None:
        for sts in self.store.list(STS_GVR)[0]:
            if sts["metadata"].get("deletionTimestamp"):
                continue
            try:
                self._reconcile(sts)
            except Exception as exc:
                self.event("Warning", "ReconcileError", str(exc), sts)

    def _reconcile(self, sts: dict) -> None:
        namespace = sts["metadata"]["namespace"]
        name = sts["metadata"]["name"]
        spec = sts.get("spec", {})
        desired = int(spec.get("replicas", 1))
        pods = [p for p in self.store.list(POD_GVR, namespace)[0]
                if _controlled_by(p, sts["metadata"]["uid"])]
        by_ordinal: dict[int, dict] = {}
        for pod in pods:
            try:
                ordinal = int(pod["metadata"]["name"].rsplit("-", 1)[1])
            except (ValueError, IndexError):
                continue
            by_ordinal[ordinal] = pod
        template = spec.get("template", {})
        revision_hash = pod_template_hash(template)
        for ordinal in range(desired):
            pod_name = f"{name}-{ordinal}"
            existing = by_ordinal.get(ordinal)
            if existing is not None and not existing["metadata"].get("deletionTimestamp"):
                continue
            if spec.get("podManagementPolicy") == "OrderedReady" and ordinal > 0:
                previous = by_ordinal.get(ordinal - 1)
                if previous is None or not _ready(previous):
                    break
            pod = self._build_pod(sts, ordinal, pod_name, revision_hash)
            if self.admission:
                from mini_k8s.admission.admission import AdmissionRequest
                try:
                    pod = self.admission.admit(AdmissionRequest(
                        "CREATE", POD_GVR, "Pod", pod, namespace=namespace, name=pod_name,
                        username="system:controller:statefulset-controller",
                        groups=["system:serviceaccounts", "system:controllers"]))
                except Exception as exc:
                    self.event("Warning", "FailedCreate", f"failed to create pod {pod_name}: {exc}", sts)
                    break
            try:
                created = self.store.create(POD_GVR, pod, kind="Pod", namespace=namespace)
                by_ordinal[ordinal] = created
            except Exception as exc:
                self.event("Warning", "FailedCreate", f"failed to create pod {pod_name}: {exc}", sts)
                break
        for ordinal in sorted([o for o in by_ordinal if o >= desired], reverse=True):
            _delete_pod(self.store, by_ordinal[ordinal])

        # Ordered, partition-aware rolling update: recreate at most one Pod per
        # reconcile, from the highest ordinal down, and only when every higher
        # ordinal is already updated and ready.
        update_strategy = spec.get("updateStrategy", {"type": "RollingUpdate"})
        partition = int(update_strategy.get("rollingUpdate", {}).get("partition", 0))
        if update_strategy.get("type", "RollingUpdate") == "RollingUpdate":
            for ordinal in range(desired - 1, partition - 1, -1):
                pod = by_ordinal.get(ordinal)
                if pod is None:
                    break
                if pod["metadata"].get("labels", {}).get("controller-revision-hash") == revision_hash:
                    continue
                higher_ready = all(
                    by_ordinal.get(h) is not None and _ready(by_ordinal[h])
                    for h in range(ordinal + 1, desired)
                )
                if higher_ready:
                    _delete_pod(self.store, pod)
                break

        current_pods = [p for p in self.store.list(POD_GVR, namespace)[0] if _controlled_by(p, sts["metadata"]["uid"])]
        ready = [p for p in current_pods if _ready(p)]
        updated = [p for p in current_pods if p["metadata"].get("labels", {}).get("controller-revision-hash") == revision_hash]
        # During a partitioned rollout, the lower ordinals still run the old
        # revision.  currentRevision must describe those Pods, while
        # updateRevision describes the template being rolled out.  MiniK8s
        # does not persist ControllerRevision objects, so the old hash is
        # recovered from the Pod labels.
        old_hashes = sorted({p["metadata"].get("labels", {}).get("controller-revision-hash")
                             for p in current_pods
                             if p["metadata"].get("labels", {}).get("controller-revision-hash") not in (None, revision_hash)})
        current_revision = old_hashes[0] if old_hashes else revision_hash
        current = [p for p in current_pods
                   if p["metadata"].get("labels", {}).get("controller-revision-hash") == current_revision]
        status = {
            "observedGeneration": sts["metadata"].get("generation", 1),
            "replicas": len(current_pods),
            "readyReplicas": len(ready),
            "currentReplicas": len(current),
            "updatedReplicas": len(updated),
            "availableReplicas": len(ready),
            "currentRevision": current_revision,
            "updateRevision": revision_hash,
        }
        self.update_status(STS_GVR, sts, status)

    def _build_pod(self, sts: dict, ordinal: int, pod_name: str, revision_hash: str) -> dict:
        namespace = sts["metadata"]["namespace"]
        spec = sts.get("spec", {})
        template = deep_copy(spec.get("template", {}))
        claim_templates = {item["metadata"]["name"]: item for item in spec.get("volumeClaimTemplates", [])}
        for template_name, claim_template in claim_templates.items():
            pvc_name = f"{template_name}-{sts['metadata']['name']}-{ordinal}"
            self._ensure_pvc(sts, claim_template, pvc_name, ordinal)
        for volume in template.get("spec", {}).get("volumes", []):
            claim = volume.get("persistentVolumeClaim", {}).get("claimName")
            if claim in claim_templates:
                volume["persistentVolumeClaim"]["claimName"] = f"{claim}-{sts['metadata']['name']}-{ordinal}"
        pod = make_pod_from_template(
            template, name=pod_name, namespace=namespace, owner=sts,
            labels={
                "statefulset.kubernetes.io/pod-name": pod_name,
                "controller-revision-hash": revision_hash,
                "apps.kubernetes.io/pod-index": str(ordinal),
            },
        )
        # StatefulSet Pods get stable per-Pod DNS names through
        # <pod>.<service>.<namespace>.svc.cluster.local.
        pod["spec"]["hostname"] = pod_name
        if spec.get("serviceName"):
            pod["spec"]["subdomain"] = spec["serviceName"]
        return pod

    def _ensure_pvc(self, sts: dict, claim_template: dict, pvc_name: str, ordinal: int) -> None:
        namespace = sts["metadata"]["namespace"]
        existing = self.store.get(PVC_GVR, namespace, pvc_name)
        if existing:
            return
        spec = deep_copy(claim_template.get("spec", {}))
        body = {
            "apiVersion": "v1", "kind": "PersistentVolumeClaim",
            "metadata": {
                "name": pvc_name, "namespace": namespace,
                "labels": {"app.kubernetes.io/name": sts["metadata"]["name"],
                           "statefulset.kubernetes.io/pod-name": f"{sts['metadata']['name']}-{ordinal}"},
            },
            "spec": spec,
        }
        try:
            self.store.create(PVC_GVR, body, kind="PersistentVolumeClaim", namespace=namespace)
        except Exception:
            pass


class DaemonSetController(Controller):
    name = "daemonset-controller"

    def __init__(self, store, *, admission=None, interval: float = 0.05):
        super().__init__(store, interval=interval)
        self.admission = admission

    def reconcile(self) -> None:
        nodes = self.store.list(NODE_GVR)[0]
        for ds in self.store.list(DS_GVR)[0]:
            if ds["metadata"].get("deletionTimestamp"):
                continue
            self._reconcile(ds, nodes)

    def _reconcile(self, ds: dict, nodes: list[dict]) -> None:
        namespace = ds["metadata"]["namespace"]
        spec = ds.get("spec", {})
        selector = spec.get("selector", {}).get("matchLabels", {})
        template_spec = spec.get("template", {}).get("spec", {})
        desired_nodes = []
        for node in nodes:
            labels = node.get("metadata", {}).get("labels", {})
            if template_spec.get("nodeSelector") and not all(
                    labels.get(k) == v for k, v in template_spec["nodeSelector"].items()):
                continue
            if not _node_affinity_ok(template_spec, node):
                continue
            # Cordoned nodes still run DaemonSet pods; the scheduler skips
            # them, but the DaemonSet controller has its own placement logic.
            tolerations = list(template_spec.get("tolerations", [])) + DS_DEFAULT_TOLERATIONS
            if any(taint.get("effect") in ("NoSchedule", "NoExecute")
                   and not _tolerates(tolerations, taint)
                   for taint in node.get("spec", {}).get("taints", [])):
                continue
            desired_nodes.append(node["metadata"]["name"])
        pods = [p for p in self.store.list(POD_GVR, namespace)[0] if _controlled_by(p, ds["metadata"]["uid"])]
        current_nodes = {p.get("spec", {}).get("nodeName") for p in pods}
        revision_hash = pod_template_hash(spec.get("template", {}))
        rolled_back = False
        for pod in pods:
            if pod.get("spec", {}).get("nodeName") in desired_nodes and pod["metadata"].get("labels", {}).get("controller-revision-hash") != revision_hash:
                if all(_ready(p) for p in pods if p is not pod):
                    _delete_pod(self.store, pod)
                    rolled_back = True
                    break
        if rolled_back:
            pods = [p for p in self.store.list(POD_GVR, namespace)[0] if _controlled_by(p, ds["metadata"]["uid"])]
            current_nodes = {p.get("spec", {}).get("nodeName") for p in pods}
        for node_name in desired_nodes:
            if node_name not in current_nodes:
                pod_name = f"{ds['metadata']['name']}-{node_name}"
                pod = make_pod_from_template(spec.get("template", {}), name=pod_name, namespace=namespace,
                                             owner=ds, node_name=node_name,
                                             labels={"controller-revision-hash": pod_template_hash(spec.get("template", {}))})
                if self.admission:
                    from mini_k8s.admission.admission import AdmissionRequest
                    try:
                        pod = self.admission.admit(AdmissionRequest(
                            "CREATE", POD_GVR, "Pod", pod, namespace=namespace, name=pod_name,
                            username="system:controller:daemonset-controller",
                            groups=["system:serviceaccounts", "system:controllers"]))
                    except Exception as exc:
                        self.event("Warning", "FailedCreate", str(exc), ds)
                        continue
                try:
                    self.store.create(POD_GVR, pod, kind="Pod", namespace=namespace)
                except Exception:
                    pass
        for pod in pods:
            if pod.get("spec", {}).get("nodeName") not in desired_nodes:
                _delete_pod(self.store, pod)
        pods = [p for p in self.store.list(POD_GVR, namespace)[0] if _controlled_by(p, ds["metadata"]["uid"])]
        status = {
            "desiredNumberScheduled": len(desired_nodes),
            "currentNumberScheduled": len({p.get("spec", {}).get("nodeName") for p in pods}),
            "numberReady": len([p for p in pods if _ready(p)]),
            "numberAvailable": len([p for p in pods if _ready(p)]),
            "numberMisscheduled": 0,
            "updatedNumberScheduled": len(pods),
            "observedGeneration": ds["metadata"].get("generation", 1),
        }
        self.update_status(DS_GVR, ds, status)


class JobController(Controller):
    name = "job-controller"

    def __init__(self, store, *, admission=None, interval: float = 0.05):
        super().__init__(store, interval=interval)
        self.admission = admission

    def reconcile(self) -> None:
        for job in self.store.list(JOB_GVR)[0]:
            if job["metadata"].get("deletionTimestamp"):
                continue
            self._reconcile_job(job)

    def _reconcile_job(self, job: dict) -> None:
        namespace = job["metadata"]["namespace"]
        spec = job.get("spec", {})
        status = job.get("status", {})
        if status.get("conditions") and any(c.get("status") == "True" and c.get("type") in ("Complete", "Failed") for c in status["conditions"]):
            ttl = spec.get("ttlSecondsAfterFinished")
            completion = parse_time(status.get("completionTime"))
            if ttl is not None and completion and (datetime.now(timezone.utc) - completion).total_seconds() >= int(ttl):
                try:
                    self.store.delete(JOB_GVR, namespace, job["metadata"]["name"], kind="Job")
                except Exception:
                    pass
            return
        if spec.get("suspend"):
            self.update_status(JOB_GVR, job, {**status, "active": 0})
            return
        pods = [p for p in self.store.list(POD_GVR, namespace)[0] if _controlled_by(p, job["metadata"]["uid"])]
        succeeded = [p for p in pods if p.get("status", {}).get("phase") == "Succeeded"]
        failed = [p for p in pods if p.get("status", {}).get("phase") == "Failed"]
        active = [p for p in pods if p.get("status", {}).get("phase") not in ("Succeeded", "Failed")]
        completions = int(spec.get("completions", 1))
        parallelism = int(spec.get("parallelism", 1))
        backoff_limit = int(spec.get("backoffLimit", 6))
        conditions = status.get("conditions", [])
        if len(succeeded) >= completions:
            conditions = set_condition(conditions, condition("Complete", True, "CompletionsReached",
                                                             f"Reached expected number of succeeded pods ({completions})"))
            conditions = [c for c in conditions if c.get("type") != "Failed"]
            self.update_status(JOB_GVR, job, {
                "startTime": status.get("startTime", now_iso()),
                "completionTime": status.get("completionTime", now_iso()),
                "succeeded": len(succeeded), "failed": len(failed), "active": len(active),
                "conditions": conditions, "ready": 0,
            })
            return
        if len(failed) > backoff_limit:
            conditions = set_condition(conditions, condition("Failed", True, "BackoffLimitExceeded",
                                                             f"Job has reached the specified backoff limit ({backoff_limit})"))
            self.update_status(JOB_GVR, job, {
                "startTime": status.get("startTime", now_iso()),
                # A failed Job is terminal and, like a successful one, gets a
                # completionTime.  ttlSecondsAfterFinished keys off it.
                "completionTime": status.get("completionTime", now_iso()),
                "succeeded": len(succeeded), "failed": len(failed), "active": len(active),
                "conditions": conditions,
            })
            return
        if len(active) < parallelism and len(active) + len(succeeded) < completions:
            used_indexes: set[int] = set()
            if spec.get("completionMode") == "Indexed":
                # Failed Pods may be replaced with the same index, so only
                # active/succeeded Pods occupy an index.
                for pod in active + succeeded:
                    try:
                        used_indexes.add(int(pod["metadata"].get("labels", {})
                                             .get("batch.kubernetes.io/job-completion-index")))
                    except (TypeError, ValueError):
                        continue
            for _ in range(min(parallelism - len(active), completions - len(active) - len(succeeded))):
                name = f"{job['metadata']['name']}-{uuid.uuid4().hex[:5]}"
                labels = {}
                if spec.get("completionMode") == "Indexed":
                    # Reuse the lowest free index after a failure; counting
                    # active+succeeded duplicates indexes when a Pod fails.
                    index = next((i for i in range(completions) if i not in used_indexes), None)
                    if index is None:
                        break
                    used_indexes.add(index)
                    labels["batch.kubernetes.io/job-completion-index"] = str(index)
                pod = make_pod_from_template(spec.get("template", {}), name=name, namespace=namespace,
                                             owner=job, labels=labels)
                if self.admission:
                    from mini_k8s.admission.admission import AdmissionRequest
                    try:
                        pod = self.admission.admit(AdmissionRequest(
                            "CREATE", POD_GVR, "Pod", pod, namespace=namespace, name=name,
                            username="system:controller:job-controller",
                            groups=["system:serviceaccounts", "system:controllers"]))
                    except Exception as exc:
                        self.event("Warning", "FailedCreate", str(exc), job)
                        break
                try:
                    created = self.store.create(POD_GVR, pod, kind="Pod", namespace=namespace)
                    active.append(created)
                except Exception:
                    pass
        self.update_status(JOB_GVR, job, {
            "startTime": status.get("startTime", now_iso()),
            "succeeded": len(succeeded), "failed": len(failed), "active": len(active),
            "conditions": conditions, "ready": len([p for p in active if _ready(p)]),
        })


class CronJobController(Controller):
    name = "cronjob-controller"

    def __init__(self, store, *, admission=None, interval: float = 0.2, time_fn: Callable[[], datetime] | None = None):
        super().__init__(store, interval=interval)
        self.admission = admission
        self.time_fn = time_fn or (lambda: datetime.now(timezone.utc))

    def reconcile(self) -> None:
        now = self.time_fn().replace(second=0, microsecond=0)
        for cronjob in self.store.list(CRONJOB_GVR)[0]:
            if cronjob["metadata"].get("deletionTimestamp"):
                continue
            self._reconcile_cronjob(cronjob, now)

    def _owned_jobs(self, namespace: str, cronjob: dict) -> list[dict]:
        return [j for j in self.store.list(JOB_GVR, namespace)[0]
                if any(ref.get("uid") == cronjob["metadata"]["uid"] for ref in j["metadata"].get("ownerReferences", []))]

    @staticmethod
    def _active_refs(jobs: list[dict], namespace: str) -> list[dict]:
        active = []
        for job in jobs:
            conditions = job.get("status", {}).get("conditions", [])
            if any(c.get("type") in ("Complete", "Failed") and c.get("status") == "True" for c in conditions):
                continue
            active.append({"kind": "Job", "name": job["metadata"]["name"],
                           "namespace": namespace, "uid": job["metadata"].get("uid", "")})
        return sorted(active, key=lambda ref: ref["name"])

    def _sync_active(self, cronjob: dict, status: dict) -> dict:
        """Keep status.active in sync even when no new Job is due.

        A completed Job must disappear from status.active on the next
        reconcile; otherwise every consumer sees a stale "running" job until
        the next schedule.
        """
        namespace = cronjob["metadata"]["namespace"]
        active = self._active_refs(self._owned_jobs(namespace, cronjob), namespace)
        if status.get("active", []) != active:
            updated = {**status, "active": active}
            self.update_status(CRONJOB_GVR, cronjob, updated)
            return updated
        return status

    def _reconcile_cronjob(self, cronjob: dict, now: datetime) -> None:
        namespace = cronjob["metadata"]["namespace"]
        spec = cronjob.get("spec", {})
        status = self._sync_active(cronjob, cronjob.get("status", {}))
        if spec.get("suspend"):
            return
        try:
            schedule = parse_cron(spec.get("schedule", ""))
        except ValueError as exc:
            self.event("Warning", "InvalidSchedule", str(exc), cronjob)
            return
        last = parse_time(status.get("lastScheduleTime"))
        if last is None:
            # On first observation, initialize to the current minute without
            # firing a job in the past.  A real CronJob controller starts its
            # scheduling window at creation time.
            self.update_status(CRONJOB_GVR, cronjob, {
                **status, "lastScheduleTime": now.isoformat().replace("+00:00", "Z"),
            })
            return
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        if now < last + timedelta(minutes=1):
            return
        due = schedule.next_after(last, inclusive=False)
        if due > now:
            return
        deadline = spec.get("startingDeadlineSeconds")
        if deadline is not None and (now - due).total_seconds() > int(deadline):
            self.update_status(CRONJOB_GVR, cronjob, {
                **status, "lastScheduleTime": now.isoformat().replace("+00:00", "Z")})
            return
        jobs = self._owned_jobs(namespace, cronjob)
        active = [j for j in jobs if not any(c.get("status") == "True" and c.get("type") in ("Complete", "Failed")
                                            for c in j.get("status", {}).get("conditions", []))]
        policy = spec.get("concurrencyPolicy", "Allow")
        if active and policy == "Forbid":
            self.update_status(CRONJOB_GVR, cronjob, {
                **status, "lastScheduleTime": now.isoformat().replace("+00:00", "Z")})
            return
        if active and policy == "Replace":
            for job in active:
                try:
                    self.store.delete(JOB_GVR, namespace, job["metadata"]["name"], kind="Job")
                except Exception:
                    pass
        job_name = f"{cronjob['metadata']['name']}-{int(due.timestamp())}"
        job_template = deep_copy(spec.get("jobTemplate", {}))
        job_body = {
            "apiVersion": "batch/v1", "kind": "Job",
            "metadata": {
                "name": job_name, "namespace": namespace,
                "labels": {"batch.kubernetes.io/cronjob": cronjob["metadata"]["name"]},
                "ownerReferences": [owner_reference(cronjob)],
            },
            "spec": job_template.get("spec", {}),
        }
        if self.admission:
            from mini_k8s.admission.admission import AdmissionRequest
            try:
                job_body = self.admission.admit(AdmissionRequest(
                    "CREATE", JOB_GVR, "Job", job_body, namespace=namespace, name=job_name,
                    username="system:controller:cronjob-controller",
                    groups=["system:serviceaccounts", "system:controllers"]))
            except Exception as exc:
                self.event("Warning", "FailedCreate", str(exc), cronjob)
                return
        try:
            self.store.create(JOB_GVR, job_body, kind="Job", namespace=namespace)
        except Exception as exc:
            self.event("Warning", "FailedCreate", str(exc), cronjob)
            return
        last_scheduled = due.isoformat().replace("+00:00", "Z")
        active_jobs = self._active_refs(self._owned_jobs(namespace, cronjob), namespace)
        self.update_status(CRONJOB_GVR, cronjob, {**status, "lastScheduleTime": last_scheduled,
                                                  "active": active_jobs})
        self._cleanup_history(namespace, cronjob, jobs, spec)
        self.event("Normal", "SuccessfulCreate", f"Created job {job_name}", cronjob)

    def _cleanup_history(self, namespace: str, cronjob: dict, jobs: list[dict], spec: dict) -> None:
        successful_limit = int(spec.get("successfulJobsHistoryLimit", 3))
        failed_limit = int(spec.get("failedJobsHistoryLimit", 1))
        successful = []
        failed = []
        for job in jobs:
            conditions = job.get("status", {}).get("conditions", [])
            if any(c.get("type") == "Complete" and c.get("status") == "True" for c in conditions):
                successful.append(job)
            elif any(c.get("type") == "Failed" and c.get("status") == "True" for c in conditions):
                failed.append(job)
        for job in sorted(successful, key=lambda j: j["metadata"].get("creationTimestamp", ""), reverse=True)[successful_limit:]:
            try:
                self.store.delete(JOB_GVR, namespace, job["metadata"]["name"], kind="Job")
            except Exception:
                pass
        for job in sorted(failed, key=lambda j: j["metadata"].get("creationTimestamp", ""), reverse=True)[failed_limit:]:
            try:
                self.store.delete(JOB_GVR, namespace, job["metadata"]["name"], kind="Job")
            except Exception:
                pass
