#!/usr/bin/env python3
"""A deterministic model of the Kubernetes elasticity stack around KubeRay.

Run from the tutorial root:

    python examples/elasticity_lab.py

KubeRay's autoscaler only changes ``replicas``.  Whether those replicas ever
become running Pods depends on a second control loop: kube-scheduler places
them, and a node autoscaler (Cluster Autoscaler, Karpenter, GKE Autopilot,
... ) adds or removes machines.  This lab reproduces that loop without a
cluster:

* cost-aware node-pool expansion for unschedulable Pods;
* bin packing, GPU fragmentation and preemption by PriorityClass;
* idle scale-down with a configurable delay;
* PodDisruptionBudgets during node drain and Spot interruption;
* ResourceQuota as a hard ceiling that no amount of node capacity can fix.

Every decision is deterministic.  The numbers are synthetic but the control
flow and the failure modes match a real cluster.
"""

from __future__ import annotations

from dataclasses import dataclass, field

MIB = 1
GIB = 1024
HOUR = 3600


@dataclass(frozen=True)
class InstanceType:
    """A machine shape offered by one node pool."""

    name: str
    cpu: int  # millicores
    memory: int  # MiB
    gpu: int
    cost_per_hour: float
    spot: bool = False


@dataclass(frozen=True)
class Pod:
    """The scheduling-relevant part of a Pod."""

    name: str
    cpu: int
    memory: int
    gpu: int = 0
    priority: int = 0
    tolerates_spot: bool = False
    node_selector: tuple[tuple[str, str], ...] = ()
    pdb_group: str = ""


@dataclass
class Node:
    name: str
    instance: InstanceType
    pool: str
    zone: str
    labels: dict[str, str] = field(default_factory=dict)
    pods: list[Pod] = field(default_factory=list)
    idle_since: int | None = None
    # When the Spot two-minute warning was received (wall-clock second).
    interrupted_at: int | None = None
    # Set once the warning has expired and the machine disappears.
    gone: bool = False

    @property
    def spot(self) -> bool:
        return self.instance.spot

    @property
    def cost_per_hour(self) -> float:
        return self.instance.cost_per_hour


@dataclass(frozen=True)
class NodePool:
    name: str
    instance_types: tuple[InstanceType, ...]
    min_size: int
    max_size: int
    zones: tuple[str, ...] = ("a", "b", "c")
    labels: tuple[tuple[str, str], ...] = ()
    # Optional capacity reservation: the sum of Pod requests that may land on
    # this pool.  This models a cloud quota, not a Kubernetes ResourceQuota.
    cpu_cap: int | None = None


def node_used(node: Node) -> tuple[int, int, int]:
    cpu = sum(pod.cpu for pod in node.pods)
    memory = sum(pod.memory for pod in node.pods)
    gpu = sum(pod.gpu for pod in node.pods)
    return cpu, memory, gpu


def node_fits(node: Node, pod: Pod) -> bool:
    if node.gone:
        return False
    # A Spot notice cordons the node immediately: no new Pods are scheduled
    # onto a machine that is about to disappear.
    if node.interrupted_at is not None:
        return False
    if node.spot and not pod.tolerates_spot:
        return False
    for key, value in pod.node_selector:
        if node.labels.get(key) != value:
            return False
    used_cpu, used_memory, used_gpu = node_used(node)
    return (
        used_cpu + pod.cpu <= node.instance.cpu
        and used_memory + pod.memory <= node.instance.memory
        and used_gpu + pod.gpu <= node.instance.gpu
    )


def instance_fits(instance: InstanceType, pod: Pod) -> bool:
    return (
        pod.cpu <= instance.cpu
        and pod.memory <= instance.memory
        and pod.gpu <= instance.gpu
        and (not instance.spot or pod.tolerates_spot)
    )


def pods_per_instance(instance: InstanceType, pod: Pod) -> int:
    """How many identical Pods fit on an empty machine (upper bound)."""
    limits = []
    if pod.cpu:
        limits.append(instance.cpu // pod.cpu)
    if pod.memory:
        limits.append(instance.memory // pod.memory)
    if pod.gpu:
        limits.append(instance.gpu // pod.gpu)
    return max(1, min(limits)) if limits else 1


@dataclass
class DisruptionBudget:
    """A simplified PodDisruptionBudget for voluntary disruptions."""

    name: str
    min_available: int
    # Every Pod in a PDB group is considered ready unless it was evicted.
    evicted: set[str] = field(default_factory=set)

    def ready(self, pods: list[Pod], group: str) -> int:
        return sum(1 for pod in pods if pod.pdb_group == group and pod.name not in self.evicted)

    def can_evict(self, pods: list[Pod], pod: Pod) -> bool:
        if not pod.pdb_group:
            return True
        return self.ready(pods, pod.pdb_group) - 1 >= self.min_available


class Fleet:
    """Nodes, Pods, a scheduler, and a node autoscaler in one deterministic loop."""

    def __init__(
        self,
        pools: list[NodePool],
        *,
        scale_down_delay: int = 600,
        quota_cpu: int | None = None,
        quota_memory: int | None = None,
    ):
        self.pools = {pool.name: pool for pool in pools}
        self.nodes: list[Node] = []
        self.pending: list[Pod] = []
        self.scheduled: list[Pod] = []
        self.evicted: list[Pod] = []
        self.scale_down_delay = scale_down_delay
        self.quota_cpu = quota_cpu
        self.quota_memory = quota_memory
        self.budgets: dict[str, DisruptionBudget] = {}
        self.events: list[str] = []
        self._node_seq = 0
        # Start from each pool's minimum size.
        for pool in pools:
            for _ in range(pool.min_size):
                self._add_node(pool, self._cheapest(pool), zone=pool.zones[0])

    # ------------------------------------------------------------------ setup
    def add_budget(self, budget: DisruptionBudget) -> None:
        self.budgets[budget.name] = budget

    def _cheapest(self, pool: NodePool) -> InstanceType:
        return min(pool.instance_types, key=lambda item: item.cost_per_hour)

    def _add_node(self, pool: NodePool, instance: InstanceType, *, zone: str | None = None) -> Node:
        self._node_seq += 1
        zone = zone or pool.zones[self._node_seq % len(pool.zones)]
        labels = dict(pool.labels)
        labels.setdefault("topology.kubernetes.io/zone", zone)
        labels.setdefault("node-pool", pool.name)
        if instance.spot:
            labels["karpenter.sh/capacity-type"] = "spot"
        else:
            labels["karpenter.sh/capacity-type"] = "on-demand"
        node = Node(
            name=f"{pool.name}-{self._node_seq}",
            instance=instance,
            pool=pool.name,
            zone=zone,
            labels=labels,
        )
        self.nodes.append(node)
        self.events.append(f"node {node.name} added ({instance.name}, {zone})")
        return node

    def pool_nodes(self, pool_name: str) -> list[Node]:
        return [node for node in self.nodes if node.pool == pool_name and not node.gone]

    # ----------------------------------------------------------------- quota
    def quota_used(self) -> tuple[int, int]:
        all_pods = self.scheduled + self.pending
        return sum(pod.cpu for pod in all_pods), sum(pod.memory for pod in all_pods)

    def quota_allows(self, pod: Pod) -> bool:
        """May this Pod be admitted under the namespace quota?

        Pending Pods in this model are a queue of *desired* Pods, not objects
        already accepted by the API server.  Only admitted (scheduled) Pods
        consume the ResourceQuota until the next one is admitted.
        """
        cpu = sum(item.cpu for item in self.scheduled)
        memory = sum(item.memory for item in self.scheduled)
        if self.quota_cpu is not None and cpu + pod.cpu > self.quota_cpu:
            return False
        if self.quota_memory is not None and memory + pod.memory > self.quota_memory:
            return False
        return True

    # ------------------------------------------------------------- scheduling
    def _place(self, pod: Pod) -> Node | None:
        candidates = [node for node in self.nodes if node_fits(node, pod)]
        if not candidates:
            return None
        # Best fit: leave the least room behind.  This is what keeps GPU
        # fragments usable for as long as possible.
        candidates.sort(key=lambda node: (
            node.instance.cpu - node_used(node)[0] - pod.cpu,
            node.instance.memory - node_used(node)[1] - pod.memory,
            node.name,
        ))
        node = candidates[0]
        node.pods.append(pod)
        node.idle_since = None
        return node

    def _preempt(self, pod: Pod) -> Node | None:
        """Evict lower-priority Pods from one node (Kubernetes preemption)."""
        for node in sorted(self.nodes, key=lambda item: item.name):
            if node.gone or node.spot and not pod.tolerates_spot:
                continue
            for key, value in pod.node_selector:
                if node.labels.get(key) != value:
                    break
            else:
                victims = sorted(
                    (item for item in node.pods if item.priority < pod.priority),
                    key=lambda item: (item.priority, item.name),
                )
                if not victims:
                    continue
                # Try the smallest victim set that makes the Pod fit.
                for count in range(1, len(victims) + 1):
                    trial = list(victims[:count])
                    remaining = [item for item in node.pods if item not in trial]
                    used_cpu = sum(item.cpu for item in remaining)
                    used_memory = sum(item.memory for item in remaining)
                    used_gpu = sum(item.gpu for item in remaining)
                    if (
                        used_cpu + pod.cpu <= node.instance.cpu
                        and used_memory + pod.memory <= node.instance.memory
                        and used_gpu + pod.gpu <= node.instance.gpu
                    ):
                        node.pods = remaining
                        for victim in trial:
                            self.evicted.append(victim)
                            self.events.append(
                                f"preempted {victim.name} on {node.name} for {pod.name}"
                            )
                            if victim.pdb_group:
                                budget = self.budgets.get(victim.pdb_group)
                                if budget:
                                    budget.evicted.add(victim.name)
                        node.pods.append(pod)
                        return node
        return None

    def schedule(self) -> None:
        """Place pending Pods; preempt lower priorities if needed."""
        # High priority first, then large Pods first (best-fit decreasing).
        self.pending.sort(key=lambda pod: (-pod.priority, -pod.cpu, -pod.memory, pod.name))
        still_pending: list[Pod] = []
        for pod in self.pending:
            node = self._place(pod)
            if node is None:
                node = self._preempt(pod)
            if node is None:
                still_pending.append(pod)
            else:
                self.scheduled.append(pod)
                self.events.append(f"scheduled {pod.name} on {node.name}")
        self.pending = still_pending

    # ----------------------------------------------------------- scale up
    def _pool_has_room(self, pool: NodePool) -> bool:
        nodes = self.pool_nodes(pool.name)
        if len(nodes) >= pool.max_size:
            return False
        if pool.cpu_cap is not None:
            reserved = sum(node.instance.cpu for node in nodes)
            if reserved >= pool.cpu_cap:
                return False
        return True

    def _choose_instance(self, pool: NodePool, pod: Pod) -> InstanceType | None:
        candidates = [item for item in pool.instance_types if instance_fits(item, pod)]
        if not candidates:
            return None
        # Cost per Pod, not cost per machine.  A bigger machine can be cheaper
        # per unit of work; Karpenter-style provisioning is allowed to see it.
        return min(
            candidates,
            key=lambda item: (
                item.cost_per_hour / pods_per_instance(item, pod),
                item.cost_per_hour,
                item.name,
            ),
        )

    def scale_up(self) -> None:
        """Add one node at a time until pending Pods fit or pools are full."""
        while self.pending:
            # Existing capacity may already absorb some (or all) of the queue;
            # always re-pack before buying another machine.
            self.schedule()
            if not self.pending:
                break
            pod = self.pending[0]
            # Respect the namespace quota before spending money on nodes.
            if not self.quota_allows(pod):
                self.events.append(f"quota blocks {pod.name}")
                break
            best: tuple[float, NodePool, InstanceType] | None = None
            for pool in self.pools.values():
                if not self._pool_has_room(pool):
                    continue
                instance = self._choose_instance(pool, pod)
                if instance is None:
                    continue
                cost = instance.cost_per_hour / pods_per_instance(instance, pod)
                if best is None or (cost, pool.name) < (best[0], best[1].name):
                    best = (cost, pool, instance)
            if best is None:
                break
            _, pool, instance = best
            self._add_node(pool, instance)
            # The next loop iteration runs schedule(), which fills the new
            # node and re-evaluates the rest of the queue.

    # ----------------------------------------------------------- scale down
    def scale_down(self, now: int) -> None:
        """Remove empty nodes after the delay, keeping each pool's minimum."""
        for node in list(self.nodes):
            if node.gone:
                continue
            if node.pods:
                node.idle_since = None
                continue
            if node.idle_since is None:
                node.idle_since = now
                continue
            if now - node.idle_since < self.scale_down_delay:
                continue
            pool = self.pools[node.pool]
            if len(self.pool_nodes(node.pool)) <= pool.min_size:
                continue
            node.gone = True
            self.events.append(f"node {node.name} removed (idle {now - node.idle_since}s)")

    # ------------------------------------------------------------ disruption
    def interrupt_spot(self, node: Node, now: int, warning_seconds: int = 120) -> None:
        """Receive the Spot interruption notice for one node."""
        node.interrupted_at = now + warning_seconds
        self.events.append(
            f"spot warning on {node.name}; {warning_seconds}s until termination"
        )

    def drain_node(self, node: Node, now: int) -> list[Pod]:
        """Evict Pods one by one, honouring PDBs.  Returns blocked Pods."""
        blocked: list[Pod] = []
        for pod in list(node.pods):
            budget = self.budgets.get(pod.pdb_group) if pod.pdb_group else None
            if budget and not budget.can_evict(self.scheduled, pod):
                blocked.append(pod)
                self.events.append(f"PDB blocks eviction of {pod.name}")
                continue
            node.pods.remove(pod)
            if pod in self.scheduled:
                self.scheduled.remove(pod)
            self.evicted.append(pod)
            if budget:
                budget.evicted.add(pod.name)
            self.events.append(f"evicted {pod.name} from {node.name}")
        return blocked

    def expire_interruptions(self, now: int) -> list[Pod]:
        """Machines whose warning expired disappear; remaining Pods are lost."""
        lost: list[Pod] = []
        for node in self.nodes:
            if node.gone or node.interrupted_at is None:
                continue
            if now < node.interrupted_at:
                continue
            for pod in list(node.pods):
                node.pods.remove(pod)
                if pod in self.scheduled:
                    self.scheduled.remove(pod)
                lost.append(pod)
                self.events.append(f"lost {pod.name} when {node.name} disappeared")
            node.gone = True
        return lost

    def replace_evicted(self) -> None:
        self.pending.extend(self.evicted)
        self.evicted = []
        self.schedule()
        # Replacement Pods are Ready as soon as they are scheduled in this
        # model.  PDB accounting tracks *currently available* Pods, so the
        # old evicted names must stop suppressing their replacements.
        for budget in self.budgets.values():
            budget.evicted.clear()

    def cost_per_hour(self) -> float:
        return round(sum(node.cost_per_hour for node in self.nodes if not node.gone), 4)


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------
CPU_POOL = NodePool(
    name="cpu",
    instance_types=(
        InstanceType("cpu-small", 4_000, 16 * GIB, 0, 0.15),
        InstanceType("cpu-large", 16_000, 64 * GIB, 0, 0.50),
    ),
    min_size=1,
    max_size=8,
)
GPU_POOL = NodePool(
    name="gpu",
    instance_types=(InstanceType("gpu-a100-2", 16_000, 128 * GIB, 2, 4.00),),
    min_size=0,
    max_size=4,
    labels=(("accelerator", "a100"),),
)
SPOT_POOL = NodePool(
    # 8 CPU / 32 GiB: two workers per machine, which makes the PDB math
    # visible (with four per machine a single drain already crosses minAvailable).
    name="spot",
    instance_types=(InstanceType("spot-large", 8_000, 32 * GIB, 0, 0.15, spot=True),),
    min_size=0,
    max_size=6,
)


def scenario_scale_up() -> None:
    fleet = Fleet([CPU_POOL])
    pods = [Pod(f"worker-{index}", 4_000, 8 * GIB) for index in range(8)]
    fleet.pending.extend(pods)
    fleet.schedule()
    fleet.scale_up()
    assert not fleet.pending, [pod.name for pod in fleet.pending]
    assert len(fleet.scheduled) == 8
    # 8 x (4 CPU / 8 GiB).  small fits 1 Pod, large fits 4.  Cost per Pod:
    # small = 0.15, large = 0.125, so the cost-aware autoscaler picks large.
    assert any(node.instance.name == "cpu-large" for node in fleet.nodes)
    print(
        f"[scale-up] 8 workers -> nodes={len([n for n in fleet.nodes if not n.gone])} "
        f"cost=${fleet.cost_per_hour():.2f}/h"
    )


def scenario_scale_down() -> None:
    fleet = Fleet([CPU_POOL], scale_down_delay=600)
    fleet.pending.extend(Pod(f"worker-{index}", 4_000, 8 * GIB) for index in range(4))
    fleet.schedule()
    fleet.scale_up()
    before = len([node for node in fleet.nodes if not node.gone])
    assert before > 1
    # All work finishes at t=1000.
    for node in fleet.nodes:
        node.pods.clear()
    fleet.scheduled.clear()
    fleet.scale_down(1_000)
    assert len([node for node in fleet.nodes if not node.gone]) == before
    fleet.scale_down(1_600)
    after = len([node for node in fleet.nodes if not node.gone])
    assert after == CPU_POOL.min_size, after
    print(f"[scale-down] nodes {before} -> {after} after 600s idle")


def scenario_gpu_fragmentation() -> None:
    fleet = Fleet([GPU_POOL])
    fleet.pending.extend([
        Pod("gpu-1", 4_000, 16 * GIB, gpu=1, node_selector=(("accelerator", "a100"),)),
        Pod("gpu-2", 4_000, 16 * GIB, gpu=1, node_selector=(("accelerator", "a100"),)),
    ])
    fleet.schedule()
    fleet.scale_up()
    assert len([node for node in fleet.nodes if not node.gone]) == 1
    # One 2-GPU Pod cannot share the machine any more.
    fleet.pending.append(Pod("gpu-3", 4_000, 16 * GIB, gpu=2, node_selector=(("accelerator", "a100"),)))
    fleet.schedule()
    fleet.scale_up()
    assert len([node for node in fleet.nodes if not node.gone]) == 2
    assert not fleet.pending
    print("[gpu] two 1-GPU Pods packed together; a 2-GPU Pod forced a second node")


def scenario_preemption() -> None:
    fleet = Fleet([CPU_POOL])
    low = [Pod(f"low-{index}", 4_000, 8 * GIB, priority=0) for index in range(4)]
    fleet.pending.extend(low)
    fleet.schedule()
    fleet.scale_up()
    high = Pod("high-priority", 8_000, 16 * GIB, priority=100)
    fleet.pending.append(high)
    fleet.schedule()
    assert high in fleet.scheduled
    assert fleet.evicted, "preemption should evict lower-priority workers"
    print(
        f"[preemption] scheduled {high.name} by evicting "
        f"{len(fleet.evicted)} low-priority Pod(s)"
    )


def scenario_spot_pdb() -> None:
    fleet = Fleet([SPOT_POOL], scale_down_delay=600)
    fleet.add_budget(DisruptionBudget("workers", min_available=4))
    fleet.pending.extend(
        Pod(f"spot-{index}", 4_000, 8 * GIB, tolerates_spot=True, pdb_group="workers")
        for index in range(6)
    )
    fleet.schedule()
    fleet.scale_up()
    nodes = [node for node in fleet.nodes if not node.gone and node.spot]
    assert len(nodes) >= 3, len(nodes)
    first, second = nodes[0], nodes[1]
    # 6 ready, minAvailable=4: draining one node with 2 Pods is allowed...
    assert not fleet.drain_node(first, now=0)
    # ...but draining a second node at the same instant would drop to 2.
    blocked = fleet.drain_node(second, now=0)
    assert blocked, "PDB must block the second eviction"
    # Replacements arrive on the drained node, then the blocked drain can
    # proceed.  The blocked Pods never left `second`, so they are not re-queued.
    fleet.replace_evicted()
    fleet.schedule()
    fleet.scale_up()
    assert not fleet.drain_node(second, now=10)
    print("[spot+pdb] first node drained; second blocked until replacements were Ready")


def scenario_spot_warning() -> None:
    fleet = Fleet([SPOT_POOL])
    fleet.pending.extend(
        Pod(f"spot-{index}", 4_000, 8 * GIB, tolerates_spot=True) for index in range(2)
    )
    fleet.schedule()
    fleet.scale_up()
    node = [item for item in fleet.nodes if item.spot and item.pods][0]
    fleet.interrupt_spot(node, now=0, warning_seconds=120)
    fleet.drain_node(node, now=0)
    fleet.replace_evicted()
    fleet.pending.extend(Pod("replacement", 4_000, 8 * GIB, tolerates_spot=True) for _ in range(2))
    fleet.schedule()
    fleet.scale_up()
    # The warning is still active; only Pods that missed the deadline are lost.
    assert fleet.expire_interruptions(now=121) == []
    assert not fleet.pending
    print("[spot-warning] 120s notice was enough to drain and replace")


def scenario_quota() -> None:
    fleet = Fleet([CPU_POOL], quota_cpu=20_000, quota_memory=80 * GIB)
    fleet.pending.extend(Pod(f"worker-{index}", 4_000, 8 * GIB) for index in range(8))
    fleet.schedule()
    fleet.scale_up()
    # 32 CPU of Pods cannot fit into a 20 CPU quota, regardless of node count.
    # Exactly five 4-CPU Pods are admitted; the rest stay queued.
    assert len(fleet.scheduled) == 5, len(fleet.scheduled)
    assert sum(pod.cpu for pod in fleet.scheduled) == 20_000
    assert len(fleet.pending) == 3
    assert any(event.startswith("quota blocks") for event in fleet.events)
    print(
        f"[quota] scheduled {len(fleet.scheduled)}/8 workers; "
        f"{len(fleet.pending)} remain pending at the 20-CPU quota ceiling"
    )


def main() -> int:
    scenario_scale_up()
    scenario_scale_down()
    scenario_gpu_fragmentation()
    scenario_preemption()
    scenario_spot_pdb()
    scenario_spot_warning()
    scenario_quota()
    print(
        "OK: node autoscaling packed Pods cost-aware, honored quotas, "
        "preempted by priority, and respected PDBs during Spot drain."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
