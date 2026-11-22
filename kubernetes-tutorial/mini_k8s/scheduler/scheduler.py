"""A filter-and-score scheduler with taints, affinity and preemption."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any

from mini_k8s.apimachinery.meta import deep_copy, now_iso
from mini_k8s.apimachinery.quantity import parse_quantity
from mini_k8s.apimachinery.selectors import match_expression, match_labels

POD_GVR = ("", "v1", "pods")
NODE_GVR = ("", "v1", "nodes")
PVC_GVR = ("", "v1", "persistentvolumeclaims")
EVENT_GVR = ("", "v1", "events")
PDB_GVR = ("policy", "v1", "poddisruptionbudgets")


@dataclass
class NodeInfo:
    node: dict
    pods: list[dict]
    used: dict[str, float]

    @property
    def name(self) -> str:
        return self.node["metadata"]["name"]

    @property
    def allocatable(self) -> dict[str, float]:
        source = self.node.get("status", {}).get("allocatable") or self.node.get("status", {}).get("capacity") or {}
        return {key: parse_quantity(value) for key, value in source.items()}

    @property
    def free(self) -> dict[str, float]:
        return {key: value - self.used.get(key, 0.0) for key, value in self.allocatable.items()}


def pod_requests(pod: dict) -> dict[str, float]:
    spec = pod.get("spec", {})
    app: dict[str, float] = {}
    for container in spec.get("containers", []):
        for key, value in container.get("resources", {}).get("requests", {}).items():
            app[key] = app.get(key, 0.0) + parse_quantity(value)
    # Init containers run sequentially, so their effective request is the max
    # of any single init container -- not the sum, and not max(init) + sum(app).
    init: dict[str, float] = {}
    for container in spec.get("initContainers", []):
        for key, value in container.get("resources", {}).get("requests", {}).items():
            init[key] = max(init.get(key, 0.0), parse_quantity(value))
    total = {
        key: max(app.get(key, 0.0), init.get(key, 0.0))
        for key in set(app) | set(init)
    }
    # Overhead (RuntimeClass) would be added here in a production scheduler.
    return total


def pod_host_ports(pod: dict) -> list[tuple[str, int, str]]:
    ports = []
    for container in pod.get("spec", {}).get("containers", []) + pod.get("spec", {}).get("initContainers", []):
        for port in container.get("ports", []):
            if port.get("hostPort"):
                ports.append((port.get("hostIP", "0.0.0.0"), int(port["hostPort"]), port.get("protocol", "TCP")))
    return ports


def _node_ready(node: dict) -> bool:
    for condition in node.get("status", {}).get("conditions", []):
        if condition.get("type") == "Ready":
            return condition.get("status") == "True"
    return False


def _toleration_matches(toleration: dict, taint: dict) -> bool:
    effect = toleration.get("effect")
    if effect and effect != taint.get("effect"):
        return False
    operator = toleration.get("operator", "Equal")
    key = toleration.get("key")
    if operator == "Exists":
        return key in (None, "", taint.get("key"))
    return key == taint.get("key") and toleration.get("value", "") == taint.get("value", "")


def _tolerates(tolerations: list[dict], taint: dict) -> bool:
    return any(_toleration_matches(toleration, taint) for toleration in tolerations)


def _node_affinity_matches(pod: dict, node: dict) -> bool:
    affinity = pod.get("spec", {}).get("affinity", {}).get("nodeAffinity", {})
    required = affinity.get("requiredDuringSchedulingIgnoredDuringExecution")
    if not required:
        return True
    labels = node.get("metadata", {}).get("labels", {})
    for term in required.get("nodeSelectorTerms", []):
        expressions = term.get("matchExpressions", [])
        fields = term.get("matchFields", [])
        if all(match_expression(expr, labels) for expr in expressions):
            field_map = {"metadata.name": node["metadata"]["name"]}
            if all(match_expression(expr, field_map) for expr in fields):
                return True
    return False


def _preferred_node_score(pod: dict, node: dict) -> int:
    affinity = pod.get("spec", {}).get("affinity", {}).get("nodeAffinity", {})
    preferred = affinity.get("preferredDuringSchedulingIgnoredDuringExecution", [])
    labels = node.get("metadata", {}).get("labels", {})
    score = 0
    for preference in preferred:
        term = preference.get("preference", {})
        expressions = term.get("matchExpressions", [])
        if all(match_expression(expr, labels) for expr in expressions):
            score += int(preference.get("weight", 0))
    return score


class Scheduler:
    def __init__(self, store, *, interval: float = 0.05, preemption: bool = True):
        self.store = store
        self.interval = interval
        self.preemption = preemption
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_node: str | None = None
        self.scheduled = 0

    def start(self) -> "Scheduler":
        if self._thread and self._thread.is_alive():
            return self
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="mini-k8s-scheduler", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)
            self._thread = None

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.schedule_once()
            except Exception as exc:  # keep the loop alive; a real scheduler exports metrics
                self._event("Warning", "SchedulerError", f"scheduler loop error: {exc}")
            self._stop.wait(self.interval)

    # ------------------------------------------------------------------ main loop
    def schedule_once(self) -> list[dict]:
        """Schedule every eligible pending pod once; returns bindings made."""
        pods = self.store.list(POD_GVR)[0]
        nodes = self.store.list(NODE_GVR)[0]
        if not nodes:
            return []
        assigned: dict[str, list[dict]] = {node["metadata"]["name"]: [] for node in nodes}
        for pod in pods:
            node_name = pod.get("spec", {}).get("nodeName")
            if node_name and node_name in assigned and not _terminal(pod):
                assigned[node_name].append(pod)
        node_infos = []
        for node in nodes:
            used: dict[str, float] = {}
            for pod in assigned.get(node["metadata"]["name"], []):
                for key, value in pod_requests(pod).items():
                    used[key] = used.get(key, 0.0) + value
            node_infos.append(NodeInfo(node, assigned.get(node["metadata"]["name"], []), used))

        bindings = []
        pending = [
            p for p in pods
            if not p.get("spec", {}).get("nodeName") and not _terminal(p)
            and not p.get("metadata", {}).get("deletionTimestamp")
            and (p.get("spec", {}).get("schedulerName") or "mini-scheduler")
                in ("mini-scheduler", "default-scheduler")
        ]
        pending.sort(key=lambda p: (-int(p.get("spec", {}).get("priority", 0)), p["metadata"]["creationTimestamp"],
                                   p["metadata"]["name"]))
        for pod in pending:
            candidates = [info for info in node_infos if self._fits(pod, info, node_infos)]
            if not candidates:
                if self.preemption and self._preempt(pod, node_infos):
                    continue
                message = f"0/{len(nodes)} nodes are available: no matching node"
                self._set_scheduled_condition(pod, False, message)
                self._event("Warning", "FailedScheduling", message, pod)
                continue
            chosen = max(candidates, key=lambda info: (self._score(pod, info, node_infos), info.name))
            updated = deep_copy(pod)
            updated["spec"]["nodeName"] = chosen.name
            updated["metadata"].setdefault("annotations", {})["scheduler.mini-k8s.io/bound-at"] = now_iso()
            self.store.update(POD_GVR, updated, kind="Pod", namespace=pod["metadata"].get("namespace", "default"))
            self._set_scheduled_condition(updated, True, f"Successfully assigned to {chosen.name}")
            chosen.pods.append(updated)
            for key, value in pod_requests(pod).items():
                chosen.used[key] = chosen.used.get(key, 0.0) + value
            self.last_node = chosen.name
            self.scheduled += 1
            self._event("Normal", "Scheduled", f"Successfully assigned {pod['metadata']['namespace']}/{pod['metadata']['name']} to {chosen.name}", updated)
            bindings.append(updated)
        return bindings

    def _set_scheduled_condition(self, pod: dict, ok: bool, message: str) -> None:
        updated = deep_copy(pod)
        status = updated.setdefault("status", {})
        conditions = [c for c in status.get("conditions", []) if c.get("type") != "PodScheduled"]
        conditions.append({
            "type": "PodScheduled",
            "status": "True" if ok else "False",
            "reason": "Scheduled" if ok else "Unschedulable",
            "message": message,
            "lastTransitionTime": now_iso(),
        })
        status["conditions"] = conditions
        try:
            self.store.update_status(
                POD_GVR, pod["metadata"].get("namespace", "default"),
                pod["metadata"]["name"], status, kind="Pod",
            )
        except Exception:
            pass

    # ------------------------------------------------------------------- filters
    def _fits(self, pod: dict, info: NodeInfo, all_nodes: list[NodeInfo]) -> bool:
        node = info.node
        if not _node_ready(node):
            return False
        if node.get("spec", {}).get("unschedulable"):
            return False
        if not self._selector_matches(pod, node):
            return False
        if not _node_affinity_matches(pod, node):
            return False
        if not self._taints_match(pod, node):
            return False
        if not self._resources_fit(pod, info):
            return False
        if not self._volumes_fit(pod, node):
            return False
        if not self._ports_fit(pod, info):
            return False
        if not self._interpod_affinity_matches(pod, info, all_nodes):
            return False
        if not self._topology_spread_matches(pod, info, all_nodes):
            return False
        return True

    @staticmethod
    def _selector_matches(pod: dict, node: dict) -> bool:
        selector = pod.get("spec", {}).get("nodeSelector", {})
        labels = node.get("metadata", {}).get("labels", {})
        return all(labels.get(key) == value for key, value in selector.items())

    @staticmethod
    def _taints_match(pod: dict, node: dict) -> bool:
        tolerations = pod.get("spec", {}).get("tolerations", [])
        for taint in node.get("spec", {}).get("taints", []):
            if taint.get("effect") in ("NoSchedule", "NoExecute") and not _tolerates(tolerations, taint):
                return False
        return True

    @staticmethod
    def _resources_fit(pod: dict, info: NodeInfo) -> bool:
        requests = pod_requests(pod)
        allocatable = info.allocatable
        for key, value in requests.items():
            # A resource the node does not advertise is unavailable, not
            # unlimited: extended resources such as nvidia.com/gpu must be
            # explicitly registered by a device plugin before they can be
            # scheduled.  Treating a missing key as "skip" would place GPU
            # Pods on CPU-only nodes.
            if key not in allocatable:
                return False
            if info.used.get(key, 0.0) + value > allocatable[key] + 1e-9:
                return False
        max_pods = allocatable.get("pods")
        if max_pods is not None and len(info.pods) + 1 > max_pods:
            return False
        return True

    def _volumes_fit(self, pod: dict, node: dict) -> bool:
        for volume in pod.get("spec", {}).get("volumes", []):
            claim = volume.get("persistentVolumeClaim", {}).get("claimName")
            if not claim:
                continue
            pvc = self.store.get(PVC_GVR, pod["metadata"].get("namespace", "default"), claim)
            if pvc is None:
                return False
            if pvc.get("status", {}).get("phase") == "Bound":
                pv_name = pvc.get("spec", {}).get("volumeName")
                pv = self.store.get(("", "v1", "persistentvolumes"), "", pv_name) if pv_name else None
                if pv:
                    node_affinity = pv.get("spec", {}).get("nodeAffinity", {}).get(
                        "required", {}).get("nodeSelectorTerms", [])
                    if node_affinity:
                        labels = node.get("metadata", {}).get("labels", {})
                        if not any(all(match_expression(expr, labels) for expr in term.get("matchExpressions", []))
                                   for term in node_affinity):
                            return False
                continue
            storage_class_name = pvc.get("spec", {}).get("storageClassName")
            storage_class = self.store.get(("storage.k8s.io", "v1", "storageclasses"), "", storage_class_name or "") if storage_class_name else None
            if storage_class and storage_class.get("volumeBindingMode") == "WaitForFirstConsumer":
                continue
            return False
        return True

    @staticmethod
    def _ports_fit(pod: dict, info: NodeInfo) -> bool:
        wanted = pod_host_ports(pod)
        if not wanted:
            return True
        used: list[tuple[str, int, str]] = []
        for existing in info.pods:
            used.extend(pod_host_ports(existing))
        for host_ip, port, protocol in wanted:
            for other_ip, other_port, other_protocol in used:
                if port == other_port and protocol == other_protocol and (host_ip == other_ip or "0.0.0.0" in (host_ip, other_ip)):
                    return False
        return True

    def _interpod_affinity_matches(self, pod: dict, info: NodeInfo, all_nodes: list[NodeInfo]) -> bool:
        affinity = pod.get("spec", {}).get("affinity", {})
        for kind in ("podAffinity", "podAntiAffinity"):
            section = affinity.get(kind, {})
            required_terms = section.get("requiredDuringSchedulingIgnoredDuringExecution", [])
            for term in required_terms:
                selector = term.get("labelSelector", {})
                topology_key = term.get("topologyKey", "kubernetes.io/hostname")
                namespaces = term.get("namespaces") or [pod["metadata"].get("namespace", "default")]
                matching_domains = set()
                for node_info in all_nodes:
                    domain = node_info.node.get("metadata", {}).get("labels", {}).get(topology_key)
                    for other in node_info.pods:
                        if other["metadata"].get("namespace") not in namespaces:
                            continue
                        if _selector_matches(selector, other.get("metadata", {}).get("labels", {})):
                            matching_domains.add(domain)
                candidate_domain = info.node.get("metadata", {}).get("labels", {}).get(topology_key)
                if kind == "podAffinity" and candidate_domain not in matching_domains:
                    return False
                if kind == "podAntiAffinity" and candidate_domain in matching_domains:
                    # The domain may contain a matching Pod on a different
                    # node; checking only info.pods misses that case.
                    return False
        return True

    def _topology_spread_matches(self, pod: dict, info: NodeInfo, all_nodes: list[NodeInfo]) -> bool:
        constraints = pod.get("spec", {}).get("topologySpreadConstraints", [])
        for constraint in constraints:
            if constraint.get("whenUnsatisfiable") != "DoNotSchedule":
                continue
            key = constraint.get("topologyKey", "kubernetes.io/hostname")
            domain = info.node.get("metadata", {}).get("labels", {}).get(key)
            selector = constraint.get("labelSelector", {})
            max_skew = int(constraint.get("maxSkew", 1))
            counts: dict[Any, int] = {}
            for node_info in all_nodes:
                d = node_info.node.get("metadata", {}).get("labels", {}).get(key)
                counts.setdefault(d, 0)
                for other in node_info.pods:
                    if _selector_matches(selector, other.get("metadata", {}).get("labels", {})):
                        counts[d] += 1
            minimum = min(counts.values()) if counts else 0
            if counts.get(domain, 0) + 1 - minimum > max_skew:
                return False
        return True

    # -------------------------------------------------------------------- scores
    def _score(self, pod: dict, info: NodeInfo, all_nodes: list[NodeInfo]) -> int:
        score = 0.0
        allocatable = info.allocatable
        cpu = allocatable.get("cpu", 0.0)
        memory = allocatable.get("memory", 0.0)
        requests = pod_requests(pod)
        if cpu > 0:
            score += 50 * (1 - (info.used.get("cpu", 0.0) + requests.get("cpu", 0.0)) / cpu)
        if memory > 0:
            score += 50 * (1 - (info.used.get("memory", 0.0) + requests.get("memory", 0.0)) / memory)
        score += _preferred_node_score(pod, info.node)
        for taint in info.node.get("spec", {}).get("taints", []):
            if taint.get("effect") == "PreferNoSchedule" and not _tolerates(pod.get("spec", {}).get("tolerations", []), taint):
                score -= 10
        # Soft pod anti-affinity: spread replicas across nodes.
        affinity = pod.get("spec", {}).get("affinity", {}).get("podAntiAffinity", {})
        for term in affinity.get("preferredDuringSchedulingIgnoredDuringExecution", []):
            selector = term.get("podTerm", term.get("podAffinityTerm", {})).get("labelSelector", {})
            if any(_selector_matches(selector, other.get("metadata", {}).get("labels", {})) for other in info.pods):
                score -= int(term.get("weight", 1))
        return int(score)

    # --------------------------------------------------------------- preemption
    def _pdb_protected(self, pod: dict) -> bool:
        """A pod cannot be a preemption victim while its PDB is exhausted."""
        namespace = pod["metadata"].get("namespace", "default")
        labels = pod.get("metadata", {}).get("labels", {})
        for pdb in self.store.list(PDB_GVR, namespace)[0]:
            selector = pdb.get("spec", {}).get("selector")
            if not selector:
                continue
            match_labels_map = selector.get("matchLabels", {})
            if match_labels_map and not all(labels.get(k) == v for k, v in match_labels_map.items()):
                continue
            expressions = selector.get("matchExpressions", [])
            if expressions and not all(match_expression(expr, labels) for expr in expressions):
                continue
            if int(pdb.get("status", {}).get("disruptionsAllowed", 1)) < 1:
                return True
        return False

    def _preempt(self, pod: dict, all_nodes: list[NodeInfo]) -> bool:
        if pod.get("spec", {}).get("preemptionPolicy", "PreemptLowerPriority") == "Never":
            return False
        priority = int(pod.get("spec", {}).get("priority", 0))
        if priority <= 0:
            return False
        for info in all_nodes:
            if not _node_ready(info.node) or info.node.get("spec", {}).get("unschedulable"):
                continue
            # Lowest priority first, then newest first, and never candidates
            # whose PDB is already exhausted.
            pool = [
                p for p in info.pods
                if int(p.get("spec", {}).get("priority", 0)) < priority
                and not p.get("metadata", {}).get("deletionTimestamp")
                and not self._pdb_protected(p)
            ]
            if not pool:
                continue
            pool.sort(key=lambda p: (int(p.get("spec", {}).get("priority", 0)),
                                     p["metadata"].get("creationTimestamp", "")))
            # Select the minimal prefix that frees enough resources.  The real
            # scheduler does a similar trial-fit while minimizing disruption.
            selected: list[dict] = []
            remaining_pods = list(info.pods)
            remaining_used = dict(info.used)
            fits = False
            for victim in pool:
                selected.append(victim)
                if victim in remaining_pods:
                    remaining_pods.remove(victim)
                for key, value in pod_requests(victim).items():
                    remaining_used[key] = remaining_used.get(key, 0.0) - value
                trial = NodeInfo(info.node, remaining_pods, remaining_used)
                if self._resources_fit(pod, trial):
                    fits = True
                    break
            if not fits:
                continue
            for victim in selected:
                self.store.delete(POD_GVR, victim["metadata"]["namespace"], victim["metadata"]["name"],
                                  kind="Pod", propagation="foreground")
                self._event("Warning", "Preempted",
                            f"Preempted by {pod['metadata']['namespace']}/{pod['metadata']['name']}", victim)
            self._event("Normal", "Preempting",
                        f"Preempting {len(selected)} pod(s) on {info.name} for higher priority pod", pod)
            return True
        return False

    # -------------------------------------------------------------------- events
    def _event(self, event_type: str, reason: str, message: str, pod: dict | None = None) -> None:
        namespace = pod["metadata"].get("namespace", "default") if pod else "kube-system"
        name = f"{pod['metadata']['name']}.{int(time.time() * 1000) % 100000}" if pod else f"scheduler.{int(time.time() * 1000) % 100000}"
        event = {
            "apiVersion": "v1", "kind": "Event",
            "metadata": {"name": name, "namespace": namespace},
            "type": event_type, "reason": reason, "message": message,
            "source": {"component": "mini-scheduler"},
            "firstTimestamp": now_iso(), "lastTimestamp": now_iso(), "count": 1,
        }
        if pod:
            event["involvedObject"] = {"kind": "Pod", "name": pod["metadata"]["name"],
                                       "namespace": namespace, "uid": pod["metadata"].get("uid", "")}
        try:
            if self.store.get(EVENT_GVR, namespace, name) is None:
                self.store.create(EVENT_GVR, event, kind="Event", namespace=namespace)
        except Exception:
            pass


def _terminal(pod: dict) -> bool:
    return pod.get("status", {}).get("phase") in ("Succeeded", "Failed")


def _selector_matches(selector: dict, labels: dict[str, str]) -> bool:
    if not selector:
        return True
    labels_map = selector.get("matchLabels", {})
    if labels_map and not all(labels.get(key) == value for key, value in labels_map.items()):
        return False
    for expression in selector.get("matchExpressions", []):
        if not match_expression(expression, labels):
            return False
    return True
