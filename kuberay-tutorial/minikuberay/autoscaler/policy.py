"""A faithful-in-spirit model of the Ray autoscaler's scale decisions.

The real algorithm lives in ``ray/autoscaler`` and is driven by pending task
and actor resource demands.  KubeRay runs it as a sidecar in the head Pod; the
sidecar patches ``spec.workerGroupSpecs[].replicas`` and uses
``scaleStrategy.workersToDelete`` to drain nodes.  MiniKubeRay runs the same
decision function in the operator process.

Autoscaler v2 can also perform In-Place Pod Resizing (IPPR).  The order is the
one upstream documents:

1. bin-pack demand against the Pods' *current* capacity;
2. bin-pack the remainder against their *maximum* capacity (``ray.io/ippr``);
3. resize the Pods that can absorb demand;
4. only then scale horizontally.

The decision function is pure: it reads the runtime and Pod objects and
returns decisions.  Applying them (patching Pods or the RayCluster spec) is the
controller's job.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .. import constants as C
from ..models.core import pod_phase
from ..models.meta import deletion_timestamp
from ..models.ippr import (
    IPPR_STATE_RESIZING,
    parse_ippr,
    pod_resources,
    retry_allowed,
    status_of,
)
from ..models.raycluster import group_desired_pods, group_replicas, ray_group_capacity
from ..runtime.cluster_runtime import RayRuntime


@dataclass
class AutoscalerDecision:
    group: str
    action: str  # "scale-up", "scale-down", "resize-up", "resize-rollback", "none"
    current: int
    desired: int
    reason: str
    workers_to_delete: list[str] | None = None
    # IPPR fields.  CPU is millicores, memory is bytes.
    pod_name: str | None = None
    current_cpu: int | None = None
    current_memory: int | None = None
    target_cpu: int | None = None
    target_memory: int | None = None
    timeout_seconds: int | None = None


class AutoscalerPolicy:
    def __init__(self, runtime: RayRuntime):
        self.runtime = runtime

    def _idle_workers(self, namespace: str, cluster: str, group: str, timeout: float) -> list[str]:
        nodes = self.runtime.ready_nodes(namespace, cluster)
        return sorted(
            node.pod_name
            for node in nodes
            if node.group == group and self.runtime.node_is_idle(node, timeout)
        )

    @staticmethod
    def _pods_by_group(pods: list[dict] | None) -> dict[str, list[dict]]:
        result: dict[str, list[dict]] = {}
        for pod in pods or []:
            if deletion_timestamp(pod):
                continue
            labels = (pod.get("metadata") or {}).get("labels") or {}
            if labels.get(C.LABEL_NODE_TYPE) != C.NODE_TYPE_WORKER:
                continue
            # Only Running Pods can be resized in place.  Pending Pods are
            # already covered by the replica arithmetic.
            if pod_phase(pod) != "Running":
                continue
            result.setdefault(labels.get(C.LABEL_GROUP, "worker"), []).append(pod)
        for items in result.values():
            items.sort(key=lambda item: item["metadata"]["name"])
        return result

    def decide(self, cluster: dict, *, pods: list[dict] | None = None) -> list[AutoscalerDecision]:
        spec = cluster.get("spec") or {}
        namespace = cluster["metadata"]["namespace"]
        name = cluster["metadata"]["name"]
        if not spec.get("enableInTreeAutoscaling") or spec.get("suspend"):
            return []
        options = spec.get("autoscalerOptions") or {}
        self._idle_timeout = float(options.get("idleTimeoutSeconds", C.DEFAULT_IDLE_TIMEOUT_SECONDS))
        mode = options.get("upscalingMode", "Default")
        demand = self.runtime.pending_demand(namespace, name)
        remaining_cpu = demand["cpu"]
        remaining_gpu = demand["gpu"]
        remaining_memory = demand.get("memory", 0.0)
        ippr_configs, _ = parse_ippr(cluster)
        live_pods = self._pods_by_group(pods)
        now = self.runtime.clock.now()
        decisions: list[AutoscalerDecision] = []
        # Actual live Ray workers per group.  ``replicas`` is the desired
        # count; after a manual scale-down or a partial deletion the two can
        # diverge, and scale-down must use the larger (actual) count or Pods
        # leak forever.
        actual_by_group: dict[str, int] = {}
        for node in self.runtime.nodes(namespace, name):
            if node.node_type == C.NODE_TYPE_WORKER and node.alive:
                actual_by_group[node.group] = actual_by_group.get(node.group, 0) + 1
        groups = list(spec.get("workerGroupSpecs") or [])
        # Higher priority groups are considered first for scale-up.
        for group in sorted(groups, key=lambda item: int(item.get("priority", 0)), reverse=True):
            group_name = group.get("groupName", "worker")
            current = group_replicas(group)
            minimum = int(group.get("minReplicas", 0) or 0)
            maximum = int(group.get("maxReplicas", C.MAX_INT32) or C.MAX_INT32)
            # Ray demand is served by the Ray container; sidecars contribute to
            # scheduling but not to Ray's logical capacity.
            capacity = ray_group_capacity(group)
            hosts = max(1, int(group.get("numOfHosts", 1) or 1))
            # ``replicas`` counts logical replicas; each replica provides
            # ``numOfHosts`` Pods, so capacity per replica is per-Pod capacity
            # times hosts.  Forgetting this over-provisions multi-host groups.
            replica_cpu = capacity.get("cpu", 0.0) * hosts
            replica_gpu = capacity.get("gpu", 0.0) * hosts
            replica_memory = capacity.get("memory", 0.0) * hosts
            idle_timeout = float(group.get("idleTimeoutSeconds", self._idle_timeout) or self._idle_timeout)
            group_pods = live_pods.get(group_name, [])
            config = ippr_configs.get(group_name)

            # Existing capacity: with IPPR the Pod may already be larger than
            # its template, so sum the live Pods instead of multiplying the
            # desired replica count.  Replicas that do not exist yet still
            # count at the template size.
            if config and group_pods:
                existing_cpu = sum(pod_resources(pod).cpu for pod in group_pods)
                existing_memory = sum(pod_resources(pod).memory for pod in group_pods)
                existing_gpu = sum(pod_resources(pod).gpu for pod in group_pods)
                # ``current`` counts replicas; each replica has ``hosts`` Pods.
                # Missing Pods are accounted at the template's per-Pod size.
                missing_pods = max(0, group_desired_pods(group) - len(group_pods))
                existing_cpu += missing_pods * capacity.get("cpu", 0.0)
                existing_memory += missing_pods * capacity.get("memory", 0.0)
                existing_gpu += missing_pods * capacity.get("gpu", 0.0)
            else:
                existing_cpu = current * replica_cpu
                existing_memory = current * replica_memory
                existing_gpu = current * replica_gpu

            remaining_cpu = max(0.0, remaining_cpu - existing_cpu)
            remaining_memory = max(0.0, remaining_memory - existing_memory)
            remaining_gpu = max(0.0, remaining_gpu - existing_gpu)

            # ---- vertical phase: absorb demand into existing Pods -----------
            if config and (remaining_cpu > 0 or remaining_memory > 0) and group_pods:
                for pod in group_pods:
                    if remaining_cpu <= 0 and remaining_memory <= 0:
                        break
                    status = status_of(pod)
                    if status.get("state") == IPPR_STATE_RESIZING:
                        continue
                    if not retry_allowed(pod, clock=self.runtime.clock):
                        continue
                    resources = pod_resources(pod)
                    cpu_headroom = config.max_cpu - resources.cpu
                    memory_headroom = config.max_memory - resources.memory
                    if cpu_headroom <= 0 and memory_headroom <= 0:
                        continue
                    absorb_cpu = min(max(0, cpu_headroom), int(math.ceil(remaining_cpu)))
                    absorb_memory = 0
                    if remaining_memory > 0 and memory_headroom > 0:
                        absorb_memory = min(memory_headroom, int(math.ceil(remaining_memory)))
                    if absorb_cpu <= 0 and absorb_memory <= 0:
                        continue
                    decisions.append(
                        AutoscalerDecision(
                            group_name,
                            "resize-up",
                            current,
                            current,
                            f"pending demand cpu={demand['cpu']:.0f}m memory={demand.get('memory', 0):.0f}B "
                            f"fits {pod['metadata']['name']} up to max",
                            pod_name=pod["metadata"]["name"],
                            current_cpu=resources.cpu,
                            current_memory=resources.memory,
                            target_cpu=resources.cpu + absorb_cpu,
                            target_memory=resources.memory + absorb_memory,
                            timeout_seconds=config.timeout_seconds,
                        )
                    )
                    remaining_cpu = max(0.0, remaining_cpu - absorb_cpu)
                    remaining_memory = max(0.0, remaining_memory - absorb_memory)

            # ---- horizontal phase -------------------------------------------
            required = 0
            if replica_cpu or replica_gpu or replica_memory:
                if remaining_cpu > 0 and replica_cpu:
                    required = max(required, math.ceil(remaining_cpu / replica_cpu))
                if remaining_memory > 0 and replica_memory:
                    required = max(required, math.ceil(remaining_memory / replica_memory))
                if remaining_gpu > 0 and replica_gpu:
                    required = max(required, math.ceil(remaining_gpu / replica_gpu))
            target = max(minimum, min(maximum, current + required))
            if target > current:
                delta = target - current
                if mode == "Conservative":
                    delta = min(delta, max(1, current))
                # Aggressive and Default both scale directly to the target in
                # KubeRay's implementation; Aggressive is an alias.
                desired = current + delta
                added = max(0, desired - current)
                consumed = min(remaining_cpu, added * replica_cpu)
                consumed_memory = min(remaining_memory, added * replica_memory)
                remaining_cpu = max(0.0, remaining_cpu - consumed)
                remaining_memory = max(0.0, remaining_memory - consumed_memory)
                remaining_gpu = max(0.0, remaining_gpu - added * replica_gpu)
                decisions.append(
                    AutoscalerDecision(group_name, "scale-up", current, desired, f"pending demand cpu={demand['cpu']:.2f} gpu={demand['gpu']:.2f}")
                )
                continue

            # Scale down only after idle timeout and never below minReplicas.
            # Never scale down while unexpired demand is still queued: the
            # demand queue keeps entries until their TTL, so shrinking would
            # immediately trigger another scale-up (flapping).
            actual = math.ceil(actual_by_group.get(group_name, 0) / hosts)
            effective_current = max(current, actual)
            in_flight_resize = any(status_of(pod).get("state") == IPPR_STATE_RESIZING for pod in group_pods)
            if (
                demand["cpu"] == 0
                and demand["gpu"] == 0
                and demand.get("memory", 0) == 0
                and effective_current > minimum
                and required < effective_current
                and not in_flight_resize
            ):
                idle = self._idle_workers(namespace, name, group_name, idle_timeout)
                # Do not remove workers that the current demand still needs.
                # When actual > desired, the surplus came from a manual
                # scale-down (or a partial deletion); never remove below the
                # user's desired count in that case.
                floor = max(minimum, required)
                if actual > current:
                    floor = max(floor, current)
                removable = min(len(idle), effective_current - floor)
                if removable > 0:
                    workers = idle[:removable]
                    decisions.append(
                        AutoscalerDecision(
                            group_name,
                            "scale-down",
                            effective_current,
                            effective_current - removable,
                            f"{removable} worker(s) idle for {idle_timeout:g}s",
                            workers,
                        )
                    )
                    continue
            decisions.append(AutoscalerDecision(group_name, "none", current, current, "steady"))
        return decisions
