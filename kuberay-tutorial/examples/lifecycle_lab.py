#!/usr/bin/env python3
"""A deterministic model of Pod termination, drains and disruption budgets.

Run from the tutorial root:

    python examples/lifecycle_lab.py

KubeRay creates Pods, but Kubernetes decides how they die.  The difference
between a 2-second scale-down and a 30-second SIGKILL is made of four knobs:

* ``terminationGracePeriodSeconds``: the total budget;
* ``preStop``: what runs before SIGTERM (for Ray workers, ``ray stop``);
* the process's SIGTERM handler (Ray's drain, Serve's request drain);
* PodDisruptionBudgets and the eviction API during node drains.

The lab models the state machine ``Running -> PreStop -> SIGTERM -> exit``
(optionally ending in SIGKILL), then applies it to head Pods, workers, Serve
replicas, rolling upgrades and Spot interruptions.

Everything is deterministic: no sleeps, no cluster, no Ray installation.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class PodSpec:
    name: str
    role: str  # "head", "worker" or "serve"
    grace: int = 30
    prestop: int = 0
    drain: int = 0
    handles_sigterm: bool = True
    pdb_group: str = ""


@dataclass
class Outcome:
    pod: PodSpec
    terminated_at: int
    exit_code: int
    sigkilled: bool
    events: list[tuple[int, str]] = field(default_factory=list)

    @property
    def duration(self) -> int:
        return self.terminated_at


def terminate(pod: PodSpec, delete_at: int = 0) -> Outcome:
    """Run the Kubernetes termination sequence for one Pod.

    The grace period starts at ``delete_at`` and includes preStop, SIGTERM
    handling and the drain.  SIGKILL happens at the deadline no matter what
    the container is doing.
    """
    deadline = delete_at + pod.grace
    events: list[tuple[int, str]] = [(delete_at, "deletionTimestamp set; endpoint removed")]

    now = delete_at
    if pod.prestop:
        now = min(now + pod.prestop, deadline)
        if now >= deadline:
            events.append((now, "preStop exceeded grace; SIGKILL"))
            return Outcome(pod, now, 137, True, events)
        events.append((now, f"preStop finished ({pod.prestop}s)"))

    events.append((now, "SIGTERM"))
    if not pod.handles_sigterm:
        events.append((deadline, "process ignored SIGTERM; SIGKILL"))
        return Outcome(pod, deadline, 137, True, events)

    finish = now + pod.drain
    if finish <= deadline:
        events.append((finish, f"drain finished ({pod.drain}s); exit 0"))
        return Outcome(pod, finish, 0, False, events)

    events.append((deadline, "drain did not finish before grace; SIGKILL"))
    return Outcome(pod, deadline, 137, True, events)


# ---------------------------------------------------------------------------
# PDB and drain model
# ---------------------------------------------------------------------------
@dataclass
class ReplicaSet:
    """Ready replicas plus a PodDisruptionBudget."""

    name: str
    ready: int
    min_available: int
    max_unavailable: int | None = None
    total: int = 0
    events: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.total:
            self.total = self.ready

    @property
    def disruptions_allowed(self) -> int:
        by_min = self.ready - self.min_available
        if self.max_unavailable is None:
            return max(0, by_min)
        return max(0, min(by_min, self.max_unavailable))

    def evict(self, count: int = 1) -> int:
        """Evict up to ``count`` Pods; return how many were allowed."""
        allowed = min(count, self.disruptions_allowed)
        self.ready -= allowed
        if allowed != count:
            self.events.append(
                f"blocked {count - allowed} eviction(s): ready={self.ready}, "
                f"minAvailable={self.min_available}"
            )
        return allowed

    def add_ready(self, count: int = 1) -> None:
        self.ready = min(self.total, self.ready + count)


@dataclass
class NodePod:
    node: str
    pod: str


def drain_nodes(rs: ReplicaSet, nodes: dict[str, list[str]], order: list[str]) -> list[tuple[int, int]]:
    """Drain nodes in order, returning ``(time, remaining_on_node)``.

    Evictions are one Pod at a time through the eviction API.  Replacements
    become Ready 20 seconds after an eviction; a blocked drain waits for them
    instead of forcing the eviction.
    """
    timeline: list[tuple[int, int]] = []
    now = 0
    replacements: list[int] = []
    for node in order:
        remaining = len(nodes.get(node, []))
        while remaining:
            while replacements and now >= replacements[0]:
                replacements.pop(0)
                rs.add_ready(1)
            if rs.evict(1):
                remaining -= 1
                replacements.append(now + 20)
                replacements.sort()
                now += 5
                timeline.append((now, remaining))
                continue
            # PDB says no.  Wait for the next replacement instead of looping
            # in virtual time.
            if replacements:
                now = max(now + 1, replacements[0])
                timeline.append((now, remaining))
            else:
                # No replacement can arrive: the drain is permanently stuck.
                timeline.append((now, remaining))
                break
    return timeline


# ---------------------------------------------------------------------------
# RayCluster availability model
# ---------------------------------------------------------------------------
@dataclass
class ClusterState:
    head: str = "Running"
    workers: list[str] = field(default_factory=list)
    gcs_ft: bool = False
    jobs_lost: int = 0

    def kill_head(self) -> int:
        """Terminate the head; return seconds of unavailability."""
        if self.gcs_ft:
            # A replacement head restores GCS from Redis/RocksDB.
            self.workers = ["reconnected" for _ in self.workers]
            self.head = "Running"
            return 15
        self.head = "Terminated"
        self.workers = ["GCS lost" for _ in self.workers]
        self.jobs_lost += 1
        return 999


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------
def scenario_graceful_worker() -> None:
    pod = PodSpec("worker-0", "worker", grace=30, prestop=2, drain=3)
    outcome = terminate(pod)
    assert outcome.exit_code == 0
    assert not outcome.sigkilled
    assert outcome.terminated_at == 5
    print("[worker] preStop 2s + drain 3s -> exit 0 at t=5, no SIGKILL")


def scenario_stuck_worker() -> None:
    pod = PodSpec("worker-stuck", "worker", grace=10, prestop=2, handles_sigterm=False)
    outcome = terminate(pod)
    assert outcome.sigkilled
    assert outcome.exit_code == 137
    assert outcome.terminated_at == 10
    print("[stuck] SIGTERM ignored -> SIGKILL at grace deadline t=10 (exit 137)")


def scenario_grace_too_short() -> None:
    pod = PodSpec("worker-slow", "worker", grace=10, prestop=2, drain=15)
    outcome = terminate(pod)
    assert outcome.sigkilled
    assert outcome.terminated_at == 10
    # Only 8 seconds remain for a 15-second drain.
    assert outcome.events[-1][1].startswith("drain did not finish")
    print("[budget] preStop 2s + drain 15s > grace 10s -> SIGKILL at t=10")


def scenario_head_without_prestop() -> None:
    # KubeRay v1.7.1 does not inject a preStop for head or worker.  A modern
    # Ray head still handles SIGTERM, so the *process* exits cleanly -- and the
    # cluster still loses GCS, because clean process exit is not durable state.
    head = PodSpec("head", "head", grace=30, prestop=0, drain=2)
    outcome = terminate(head)
    assert outcome.exit_code == 0
    assert outcome.terminated_at == 2
    cluster = ClusterState(head="Running", workers=["w0", "w1", "w2"])
    downtime = cluster.kill_head()
    assert cluster.jobs_lost == 1
    assert downtime == 999
    print("[head] graceful exit at t=2, but without GCS FT the jobs and workers are lost")


def scenario_head_gcs_ft() -> None:
    head = PodSpec("head", "head", grace=30, prestop=1, drain=2)
    outcome = terminate(head)
    assert outcome.exit_code == 0
    cluster = ClusterState(head="Running", workers=["w0", "w1", "w2"], gcs_ft=True)
    downtime = cluster.kill_head()
    assert downtime == 15
    assert cluster.jobs_lost == 0
    assert cluster.workers == ["reconnected", "reconnected", "reconnected"]
    print("[head+gcs-ft] graceful head exit; replacement head in 15s, no job loss")


def scenario_pdb_drain() -> None:
    rs = ReplicaSet("serve", ready=6, min_available=4, total=6)
    nodes = {"node-a": ["a0", "a1"], "node-b": ["b0", "b1"], "node-c": ["c0", "c1"]}
    timeline = drain_nodes(rs, nodes, ["node-a", "node-b"])
    # node-a drains by t=10: two evictions, the second after a replacement.
    assert timeline[:2] == [(5, 1), (10, 0)], timeline
    # node-b starts with 2 Pods and cannot evict either until a replacement
    # from node-a becomes Ready; ``(20, 2)`` is that waiting window.
    assert any(remaining == 2 for _, remaining in timeline), timeline
    assert rs.ready >= rs.min_available
    print(f"[pdb] drain timeline={timeline}; ready never fell below {rs.min_available}")


def scenario_rolling_upgrade() -> None:
    # maxSurge=2, maxUnavailable=0: add two new replicas, then retire old ones.
    # Once four new replicas are Ready, the remaining old ones leave without a
    # replacement, so the total returns to the desired count instead of growing.
    old_ready = 4
    new_ready = 0
    desired = 4
    demand = 4
    failed_requests = 0
    steps: list[tuple[str, int]] = []

    def capacity() -> int:
        return old_ready + new_ready

    new_ready += 2
    steps.append(("surge +2", capacity()))
    while old_ready:
        old_ready -= 1
        if new_ready < desired:
            new_ready += 1
            steps.append(("retire old, add new", capacity()))
        else:
            steps.append(("retire old", capacity()))
        if capacity() < demand:
            failed_requests += 1
    assert failed_requests == 0
    assert new_ready == desired
    assert all(value >= demand for _, value in steps)
    print(f"[rollout] maxSurge=2 maxUnavailable=0 -> capacity={steps}; failures=0")


def scenario_spot_warning() -> None:
    fast = PodSpec("spot-fast", "worker", grace=30, prestop=5, drain=10)
    fast_outcome = terminate(fast)
    assert fast_outcome.exit_code == 0
    assert fast_outcome.terminated_at == 15

    slow = PodSpec("spot-slow", "worker", grace=300, prestop=5, drain=200)
    slow_outcome = terminate(slow, delete_at=0)
    # The Pod itself would take 205s, but the Spot notice only gives 120s.
    warning = 120
    killed = slow_outcome.terminated_at > warning
    assert killed
    print(
        f"[spot] notice=120s: fast drain finished at t={fast_outcome.terminated_at}; "
        "slow drain would exceed the warning and be lost"
    )


def main() -> int:
    scenario_graceful_worker()
    scenario_stuck_worker()
    scenario_grace_too_short()
    scenario_head_without_prestop()
    scenario_head_gcs_ft()
    scenario_pdb_drain()
    scenario_rolling_upgrade()
    scenario_spot_warning()
    print(
        "OK: grace budgets, preStop/SIGTERM, head failure, PDB drains, "
        "rolling upgrades and Spot warnings are reproducible."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
