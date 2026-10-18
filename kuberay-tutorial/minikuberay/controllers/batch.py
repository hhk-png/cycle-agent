"""Simulated kubelet, scheduler, EndpointSlice controller, Job controller and GC."""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field

from .. import constants as C
from ..clock import iso, parse_iso
from ..errors import NotFound
from ..models.core import (
    job_failed,
    job_succeeded,
    new_pod,
    pod_containers,
    pod_is_ready,
    pod_phase,
    pod_requested_resources,
)
from ..models.ippr import complete_resize, fail_resize, status_of
from ..models.meta import deletion_timestamp, owner_reference, uid
from ..util import deep_copy, get_ray_container, resource_quantity
from .base import Controller


@dataclass
class SchedulerNode:
    name: str
    cpu: float
    memory: float
    gpu: float
    labels: dict[str, str] = field(default_factory=dict)
    taints: list[dict] = field(default_factory=list)


def _tolerates(toleration: dict, taint: dict) -> bool:
    """Kubernetes toleration matching (Equal/Exists, key/effect/value)."""
    operator = toleration.get("operator", "Equal")
    key = toleration.get("key")
    effect = toleration.get("effect")
    if effect not in (None, "", taint.get("effect")):
        return False
    if operator == "Exists":
        return key in (None, "") or key == taint.get("key")
    # Equal (default): an empty key only matches with Exists.
    if key in (None, "") or key != taint.get("key"):
        return False
    return toleration.get("value", "") == taint.get("value", "")


class SimulatedScheduler:
    """A tiny filter/score scheduler with resource fit and node selectors."""

    def __init__(self):
        # CPU is expressed in millicores to match parse_cpu(); memory in bytes.
        self.nodes = [
            SchedulerNode("node-a", 8000, 32 * 2**30, 0, {"kubernetes.io/hostname": "node-a", "topology.kubernetes.io/zone": "a"}),
            SchedulerNode("node-b", 8000, 32 * 2**30, 1, {"kubernetes.io/hostname": "node-b", "topology.kubernetes.io/zone": "b"}),
            SchedulerNode("node-c", 16000, 64 * 2**30, 2, {"kubernetes.io/hostname": "node-c", "topology.kubernetes.io/zone": "c"}),
        ]

    def _used(self, pods: list[dict], node_name: str) -> dict[str, float]:
        totals = {"cpu": 0.0, "memory": 0.0, "nvidia.com/gpu": 0.0}
        for pod in pods:
            if pod.get("spec", {}).get("nodeName") != node_name:
                continue
            if pod_phase(pod) in ("Succeeded", "Failed"):
                continue
            # Terminating Pods still hold resources until their finalizers are
            # cleared; do not free the capacity prematurely.

            try:
                requests = pod_requested_resources(pod)
            except (TypeError, ValueError):
                # Ignore malformed Pods when accounting; the kubelet will mark
                # them unschedulable on their own.
                continue
            for key in totals:
                totals[key] += requests.get(key, 0.0)
        return totals

    def assign(self, pod: dict, pods: list[dict]) -> str | None:
        try:
            requests = pod_requested_resources(pod)
        except (TypeError, ValueError):
            # A malformed quantity must make this Pod unschedulable, not abort
            # the whole reconcile loop for every other Pod.
            return None
        spec = pod.get("spec") or {}
        node_selector = spec.get("nodeSelector") or {}
        tolerations = spec.get("tolerations") or []
        feasible = []
        for node in self.nodes:
            for key, value in node_selector.items():
                if node.labels.get(key) != value:
                    break
            else:
                if any(
                    taint.get("effect") in ("NoSchedule", "NoExecute")
                    and not any(_tolerates(toleration, taint) for toleration in tolerations)
                    for taint in node.taints
                ):
                    continue
                used = self._used(pods, node.name)
                if all(
                    used[resource] + requests.get(resource, 0.0) <= getattr(node, attribute)
                    for resource, attribute in (("cpu", "cpu"), ("memory", "memory"), ("nvidia.com/gpu", "gpu"))
                ):
                    feasible.append((used["cpu"] + used["nvidia.com/gpu"] * 1000, node.name))
        if not feasible:
            return None
        feasible.sort()
        return feasible[0][1]


class Kubelet(Controller):
    name = "kubelet"
    kind = C.KIND_POD

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.scheduler = SimulatedScheduler()
        self._ip_counter = 10
        self._seen_pending: dict[str, int] = {}

    def _allocate_ip(self) -> str:
        self._ip_counter += 1
        return f"10.244.{(self._ip_counter // 250) % 250}.{self._ip_counter % 250}"

    def _container_status(self, container: dict, *, running: bool, exit_code: int | None = None, restart_count: int = 0) -> dict:
        if running:
            state = {"running": {"startedAt": iso(self.clock.now())}}
        elif exit_code is None:
            state = {"waiting": {"reason": "ContainerCreating"}}
        else:
            state = {"terminated": {"exitCode": exit_code, "finishedAt": iso(self.clock.now()), "reason": "Completed" if exit_code == 0 else "Error"}}
        return {
            "name": container.get("name"),
            "image": container.get("image"),
            "ready": running,
            "restartCount": restart_count,
            "state": state,
            "started": running,
        }

    def _mark_running(self, pod: dict, node_name: str) -> dict:
        pod = deep_copy(pod)
        status = pod.setdefault("status", {})
        status["phase"] = "Running"
        status["hostIP"] = self._node_ip(node_name)
        status["podIP"] = status.get("podIP") or self._allocate_ip()
        status["startTime"] = status.get("startTime") or iso(self.clock.now())
        containers = pod_containers(pod)
        status["containerStatuses"] = [self._container_status(item, running=True) for item in containers]
        status["conditions"] = [
            {"type": "PodScheduled", "status": "True", "lastTransitionTime": iso(self.clock.now()), "reason": "Scheduled"},
            {"type": "Initialized", "status": "True", "lastTransitionTime": iso(self.clock.now()), "reason": "ContainersStarted"},
            {"type": "ContainersReady", "status": "True", "lastTransitionTime": iso(self.clock.now()), "reason": "ContainersReady"},
            {"type": "Ready", "status": "True", "lastTransitionTime": iso(self.clock.now()), "reason": "ContainersReady"},
        ]
        return pod

    @staticmethod
    def _node_ip(node_name: str) -> str:
        index = {"node-a": 11, "node-b": 12, "node-c": 13}.get(node_name, 14)
        return f"192.168.1.{index}"

    def _reconcile_ippr(self, pod: dict) -> tuple[dict, bool]:
        """Complete a pending in-place resize.

        Real kubelet flow: the resize subresource updates the desired spec, the
        kubelet applies cgroup changes asynchronously, and the Pod reports
        ``PodResizeInProgress`` until the runtime confirms it.  MiniKubeRay
        models the latency with ``minikuberay.io/ippr-delay-seconds`` and lets
        tests force the failure path with ``minikuberay.io/ippr-result``.
        """
        status = status_of(pod)
        if status.get("state") != "resizing":
            return pod, False
        annotations = (pod.get("metadata") or {}).get("annotations") or {}
        result = str(annotations.get(C.ANNOTATION_SIM_IPPR_RESULT, "succeeded")).lower()
        delay = float(annotations.get(C.ANNOTATION_SIM_IPPR_DELAY, C.DEFAULT_IPPR_RESIZE_SECONDS))
        resizing_at = status.get("resizing_at")
        elapsed = (self.clock.now() - parse_iso(resizing_at)).total_seconds() if resizing_at else delay
        timeout = float(status.get("timeout_seconds", 60) or 60)
        if result == "failure":
            return fail_resize(pod, clock=self.clock, reason="ResizeFailed")
        # A test hook can force the timeout path immediately; a real kubelet
        # that never finishes must also be bounded by resize-timeout.
        if result == "timeout" and elapsed >= min(delay, timeout):
            return fail_resize(pod, clock=self.clock, reason="ResizeTimeout")
        if elapsed >= timeout:
            return fail_resize(pod, clock=self.clock, reason="ResizeTimeout")
        if elapsed >= delay:
            return complete_resize(pod, clock=self.clock)
        return pod, False

    def step(self) -> bool:
        changed = False
        pods = self.store.list(C.KIND_POD, self.config.namespace)
        for pod in pods:
            namespace = pod["metadata"].get("namespace", "default")
            name = pod["metadata"]["name"]
            if deletion_timestamp(pod):
                if pod["metadata"].get("finalizers"):
                    # A finalizer means the Pod is not gone yet: keep it
                    # terminating and keep its resources accounted for.
                    continue
                try:
                    self.store.delete(C.KIND_POD, name, namespace, force=True)
                    self._seen_pending.pop(f"{namespace}/{name}", None)
                    changed = True
                except NotFound:
                    pass
                continue

            phase = pod_phase(pod)
            if phase == "Pending":
                spec = pod.get("spec") or {}
                scheduled_now = False
                if not spec.get("nodeName"):
                    node = self.scheduler.assign(pod, pods)
                    if not node:
                        new_status = {
                            "phase": "Pending",
                            "conditions": [
                                {
                                    "type": "PodScheduled",
                                    "status": "False",
                                    "lastTransitionTime": iso(self.clock.now()),
                                    "reason": "Unschedulable",
                                    "message": "0/3 nodes are available: insufficient cpu, memory or GPU",
                                }
                            ],
                        }
                        already_unschedulable = any(
                            condition.get("type") == "PodScheduled" and condition.get("reason") == "Unschedulable"
                            for condition in (pod.get("status") or {}).get("conditions") or []
                        )
                        if not already_unschedulable:
                            pod["status"] = new_status
                            self.store.update(C.KIND_POD, pod)
                            changed = True
                        continue
                    pod["spec"]["nodeName"] = node
                    scheduled_now = True
                annotations = pod["metadata"].get("annotations") or {}
                delay = float(annotations.get(C.ANNOTATION_SIM_POD_START_DELAY, 0) or 0)
                created = pod["metadata"].get("creationTimestamp")
                elapsed = (self.clock.now() - parse_iso(created)).total_seconds() if created else 0
                # Transition on the second observation even with a manual clock,
                # so a pending Pod never deadlocks the reconcile loop.
                pending_key = f"{namespace}/{name}"
                seen = self._seen_pending.get(pending_key, 0) + 1
                self._seen_pending[pending_key] = seen
                if elapsed < delay or seen < 2:
                    # Only persist a real change (the first NodeName bind).
                    # Writing an unchanged Pending Pod every round bumps
                    # resourceVersion forever and breaks convergence.
                    if scheduled_now:
                        self.store.update(C.KIND_POD, pod)
                        changed = True
                    continue
                running = self._mark_running(pod, pod["spec"]["nodeName"])
                self.store.update(C.KIND_POD, running)
                self._seen_pending.pop(pending_key, None)
                changed = True
                labels = running["metadata"].get("labels") or {}
                # Only Ray nodes register with the runtime.  Cleanup Jobs and
                # other helper Pods may carry ray.io/cluster but have neither
                # the node-type label nor is-ray-node=yes.
                is_ray_pod = labels.get(C.LABEL_IS_RAY_NODE) == "yes" or labels.get(C.LABEL_NODE_TYPE) in (
                    C.NODE_TYPE_HEAD,
                    C.NODE_TYPE_WORKER,
                )
                if labels.get(C.LABEL_CLUSTER) and is_ray_pod:
                    self.runtime.register_pod(running)
                continue

            if phase == "Running":
                annotations = pod["metadata"].get("annotations") or {}
                labels = pod["metadata"].get("labels") or {}
                resized_pod, resized = self._reconcile_ippr(pod)
                if resized:
                    self.store.update(C.KIND_POD, resized_pod)
                    if status_of(resized_pod).get("state") == "succeeded":
                        self.runtime.update_pod_resources(resized_pod)
                    changed = True
                    pod = resized_pod
                    annotations = pod["metadata"].get("annotations") or {}
                crash_after = annotations.get(C.ANNOTATION_SIM_CRASH_AFTER)
                if crash_after is not None:
                    started = pod.get("status", {}).get("startTime") or pod["metadata"].get("creationTimestamp")
                    if started and (self.clock.now() - parse_iso(started)).total_seconds() >= float(crash_after):
                        failed = deep_copy(pod)
                        failed["status"]["phase"] = "Failed"
                        failed["status"]["reason"] = "SimulatedCrash"
                        failed["status"]["containerStatuses"] = [
                            self._container_status(item, running=False, exit_code=137) for item in pod_containers(pod)
                        ]
                        failed["status"]["conditions"] = [
                            {"type": "Ready", "status": "False", "lastTransitionTime": iso(self.clock.now()), "reason": "PodFailed"},
                            {"type": "ContainersReady", "status": "False", "lastTransitionTime": iso(self.clock.now()), "reason": "PodFailed"},
                        ]
                        self.store.update(C.KIND_POD, failed)
                        if labels.get(C.LABEL_CLUSTER):
                            self.runtime.unregister_pod(namespace, labels[C.LABEL_CLUSTER], name)
                        changed = True
                        continue

                complete_after = annotations.get(C.ANNOTATION_SIM_JOB_COMPLETE)
                if complete_after is not None:
                    started = pod.get("status", {}).get("startTime") or pod["metadata"].get("creationTimestamp")
                    if started and (self.clock.now() - parse_iso(started)).total_seconds() >= float(complete_after):
                        finished = deep_copy(pod)
                        finished["status"]["phase"] = "Succeeded"
                        finished["status"]["containerStatuses"] = [
                            self._container_status(item, running=False, exit_code=0) for item in pod_containers(pod)
                        ]
                        finished["status"]["conditions"] = [
                            {"type": "Ready", "status": "False", "lastTransitionTime": iso(self.clock.now()), "reason": "PodCompleted"},
                        ]
                        self.store.update(C.KIND_POD, finished)
                        changed = True
                        continue

                submitter_finished = str(annotations.get("minikuberay.io/submitter-finished", "")).lower() == "true"
                if submitter_finished:
                    exit_code = int(annotations.get("minikuberay.io/submitter-exit-code", 0))
                    finished = deep_copy(pod)
                    finished["status"]["phase"] = "Succeeded" if exit_code == 0 else "Failed"
                    finished["status"]["containerStatuses"] = [
                        self._container_status(item, running=False, exit_code=exit_code) for item in pod_containers(pod)
                    ]
                    finished["status"]["conditions"] = [
                        {"type": "Ready", "status": "False", "lastTransitionTime": iso(self.clock.now()), "reason": "PodCompleted"},
                    ]
                    self.store.update(C.KIND_POD, finished)
                    changed = True
                    continue

                # A pod's Ready condition can be toggled by a health-check
                # simulation; otherwise it stays ready.
                if not pod_is_ready(pod):
                    ready = deep_copy(pod)
                    for condition in ready["status"].get("conditions") or []:
                        if condition.get("type") == "Ready":
                            condition["status"] = "True"
                            condition["lastTransitionTime"] = iso(self.clock.now())
                    self.store.update(C.KIND_POD, ready)
                    changed = True
                continue

            if phase in ("Succeeded", "Failed"):
                if (pod["metadata"].get("labels") or {}).get(C.LABEL_CLUSTER):
                    self.runtime.unregister_pod(namespace, pod["metadata"]["labels"][C.LABEL_CLUSTER], name)
        return changed


class EndpointController(Controller):
    name = "endpoints"
    kind = C.KIND_SERVICE

    def reconcile(self, service: dict) -> bool:
        from .builders import build_endpoint_slice

        namespace = service["metadata"].get("namespace", "default")
        name = service["metadata"]["name"]
        pods = self.store.list(C.KIND_POD, namespace)
        # Allocate a stable cluster IP for non-headless Services.
        spec = service.setdefault("spec", {})
        if not spec.get("clusterIP") and spec.get("type", "ClusterIP") == "ClusterIP":
            digest = int(hashlib.sha256(f"{namespace}/{name}".encode()).hexdigest(), 16)
            spec["clusterIP"] = f"10.96.{(digest >> 8) % 250}.{(digest % 250) + 1}"
            self.store.update(C.KIND_SERVICE, service)
            service = self.store.get(C.KIND_SERVICE, name, namespace)
        endpoint_slice = build_endpoint_slice(service, pods)
        self.ensure(C.KIND_ENDPOINTSLICE, endpoint_slice)
        return False

    def reconcile_all(self) -> bool:
        for service in self.store.list(C.KIND_SERVICE, self.config.namespace):
            self.reconcile(service)
        return False


class BatchJobController(Controller):
    name = "batchjob"
    kind = C.KIND_JOB

    def _pod_name(self, job: dict) -> str:
        return f"{job['metadata']['name']}-submitter"

    def _ensure_pod(self, job: dict) -> bool:
        from ..models.core import new_pod
        from ..models.meta import owner_reference

        namespace = job["metadata"].get("namespace", "default")
        pod_name = self._pod_name(job)
        try:
            self.store.get(C.KIND_POD, pod_name, namespace)
            return False
        except NotFound:
            template = deep_copy((job.get("spec") or {}).get("template") or {})
            spec = template.get("spec") or {}
            labels = {**(template.get("metadata") or {}).get("labels", {}), "job-name": job["metadata"]["name"], "batch.kubernetes.io/job-name": job["metadata"]["name"]}
            pod = new_pod(
                pod_name,
                namespace,
                spec,
                labels=labels,
                annotations=(template.get("metadata") or {}).get("annotations") or {},
                owner_references=[owner_reference(job)],
            )
            self.store.create(C.KIND_POD, pod, namespace)
            job["status"]["active"] = 1
            self.store.update(C.KIND_JOB, job)
            return True

    def reconcile(self, job: dict) -> bool:
        namespace = job["metadata"].get("namespace", "default")
        name = job["metadata"]["name"]
        if deletion_timestamp(job):
            for pod in self.store.list(C.KIND_POD, namespace, label_selector={"job-name": name}):
                try:
                    self.store.delete(C.KIND_POD, pod["metadata"]["name"], namespace, force=True)
                except NotFound:
                    pass
            return False
        if (job.get("spec") or {}).get("suspend"):
            for pod in self.store.list(C.KIND_POD, namespace, label_selector={"job-name": name}):
                try:
                    self.store.delete(C.KIND_POD, pod["metadata"]["name"], namespace, force=True)
                except NotFound:
                    pass
            return False
        conditions = (job.get("status") or {}).get("conditions") or []
        if any(condition.get("type") in ("Complete", "Failed") and condition.get("status") == "True" for condition in conditions):
            return False
        pods = self.store.list(C.KIND_POD, namespace, label_selector={"job-name": name})
        if not pods:
            return self._ensure_pod(job)
        pod = pods[0]
        if pod_phase(pod) == "Succeeded" and not job_succeeded(job):
            job["status"]["succeeded"] = int(job["status"].get("succeeded", 0)) + 1
            job["status"]["active"] = 0
            job["status"].setdefault("conditions", []).append(
                {"type": "Complete", "status": "True", "lastTransitionTime": iso(self.clock.now()), "reason": "Completed"}
            )
            self.store.update(C.KIND_JOB, job)
            return True
        if pod_phase(pod) == "Failed":
            job["status"]["failed"] = int(job["status"].get("failed", 0)) + 1
            job["status"]["active"] = 0
            backoff = int((job.get("spec") or {}).get("backoffLimit", 0))
            if job["status"]["failed"] > backoff:
                job["status"].setdefault("conditions", []).append(
                    {"type": "Failed", "status": "True", "lastTransitionTime": iso(self.clock.now()), "reason": "BackoffLimitExceeded"}
                )
            else:
                try:
                    self.store.delete(C.KIND_POD, pod["metadata"]["name"], namespace, force=True)
                except NotFound:
                    pass
            self.store.update(C.KIND_JOB, job)
            return True
        return False


class GarbageCollector(Controller):
    name = "garbage-collector"
    kind = ""

    def run(self) -> bool:
        changed = False
        owner_kinds = {
            C.KIND_RAYCLUSTER,
            C.KIND_RAYJOB,
            C.KIND_RAYSERVICE,
            C.KIND_RAYCRONJOB,
            C.KIND_POD,
            C.KIND_SERVICE,
            C.KIND_ENDPOINTSLICE,
            C.KIND_JOB,
            C.KIND_SECRET,
            C.KIND_PVC,
            C.KIND_SERVICEACCOUNT,
            C.KIND_ROLE,
            C.KIND_ROLEBINDING,
            C.KIND_NETWORKPOLICY,
            C.KIND_HTTPROUTE,
            C.KIND_GATEWAY,
            C.KIND_PODGROUP,
            C.KIND_INGRESS,
        }
        namespace = self.config.namespace
        uids = set()
        for kind in owner_kinds:
            for obj in self.store.list(kind, namespace):
                uids.add(uid(obj))
        for kind in owner_kinds:
            for obj in self.store.list(kind, namespace):
                for ref in obj["metadata"].get("ownerReferences") or []:
                    if not ref.get("controller"):
                        continue
                    if ref.get("uid") not in uids:
                        try:
                            self.store.delete(kind, obj["metadata"]["name"], obj["metadata"]["namespace"])
                            changed = True
                        except NotFound:
                            pass
                        break
        # Keep event volume bounded; events are useful for demos but not forever.
        return changed
