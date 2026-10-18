#!/usr/bin/env python3
"""A deterministic model of controller-runtime reliability mechanics.

Run from the tutorial root:

    python examples/operator_reliability_lab.py

The KubeRay operator is itself a distributed system: a manager, informer
caches, a rate-limited work queue, leader election and health probes.  This
lab reproduces the failure modes that decide whether the control plane is
available:

* cache sync taking longer than its timeout (manager never becomes Ready);
* leader election handover and the split-brain window;
* workqueue retries with exponential backoff and a retry cap;
* panic recovery in one reconciler without killing the manager;
* liveness vs readiness semantics;
* a reconcile SLO and its error budget.

No Kubernetes, threads or sleeps: virtual time only.
"""

from __future__ import annotations

from dataclasses import dataclass, field


# ---------------------------------------------------------------------------
# Work queue with rate limiting
# ---------------------------------------------------------------------------
@dataclass
class WorkItem:
    key: str
    attempts: int = 0
    ready_at: float = 0.0


class WorkQueue:
    """A small subset of client-go's rate-limiting workqueue."""

    def __init__(self, *, base_delay: float = 0.005, max_delay: float = 1000.0, max_retries: int = 5):
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.max_retries = max_retries
        self._items: list[WorkItem] = []
        self._dirty: set[str] = set()
        self._processing: set[str] = set()
        self.dropped: list[str] = []
        self.delays: list[float] = []

    def add(self, key: str, now: float = 0.0) -> None:
        if key in self._dirty or key in self._processing:
            return
        self._dirty.add(key)
        self._items.append(WorkItem(key, 0, now))

    def get(self, now: float) -> WorkItem | None:
        for index, item in enumerate(self._items):
            if item.ready_at <= now:
                self._items.pop(index)
                self._dirty.discard(item.key)
                self._processing.add(item.key)
                return item
        return None

    def done(self, item: WorkItem) -> None:
        self._processing.discard(item.key)

    def retry(self, item: WorkItem, now: float) -> float | None:
        """Requeue with exponential backoff; drop after ``max_retries``."""
        self.done(item)
        item.attempts += 1
        if item.attempts > self.max_retries:
            self.dropped.append(item.key)
            return None
        delay = min(self.max_delay, self.base_delay * (2 ** (item.attempts - 1)))
        item.ready_at = now + delay
        self.delays.append(delay)
        self._dirty.add(item.key)
        self._items.append(item)
        return delay

    def forget(self, item: WorkItem) -> None:
        self.done(item)

    def depth(self) -> int:
        return len(self._items)


# ---------------------------------------------------------------------------
# Informer cache
# ---------------------------------------------------------------------------
class CacheSyncTimeout(Exception):
    pass


@dataclass
class InformerCache:
    objects: int
    objects_per_second: int
    synced: bool = False

    def sync(self, timeout: float) -> float:
        elapsed = self.objects / self.objects_per_second
        if elapsed > timeout:
            raise CacheSyncTimeout(
                f"cache sync took {elapsed:.1f}s > timeout {timeout:.1f}s"
            )
        self.synced = True
        return elapsed


# ---------------------------------------------------------------------------
# Leader election
# ---------------------------------------------------------------------------
@dataclass
class Lease:
    holder: str
    expires_at: float


@dataclass
class Candidate:
    name: str
    active: bool = False

    def renew(self, lease: Lease, now: float, duration: float) -> Lease:
        if lease.holder != self.name:
            return lease
        lease.expires_at = now + duration
        self.active = True
        return lease


def elect(candidates: list[Candidate], lease: Lease | None, now: float) -> tuple[Lease | None, Candidate | None]:
    """Try to acquire/renew; return the current lease and leader."""
    if lease and lease.expires_at > now:
        leader = next((c for c in candidates if c.name == lease.holder), None)
        if leader and leader.active:
            return lease, leader
        # The holder stopped renewing but the lease has not expired yet:
        # nobody may take over, and nobody is acting as leader.
        return lease, None
    # Lease expired (or holder is gone): rotate to the next candidate so a
    # dead leader does not immediately re-acquire its own lease.
    start = 0
    if lease:
        names = [candidate.name for candidate in candidates]
        if lease.holder in names:
            start = (names.index(lease.holder) + 1) % len(candidates)
    winner = candidates[start]
    for candidate in candidates:
        candidate.active = candidate is winner
    return Lease(winner.name, now + 15.0), winner


# ---------------------------------------------------------------------------
# Manager probes and panic recovery
# ---------------------------------------------------------------------------
@dataclass
class Manager:
    cache_sync_timeout: float = 120.0
    liveness_timeout: float = 30.0
    last_heartbeat: float = 0.0
    synced: bool = False
    shutting_down: bool = False
    panics: int = 0

    def ready(self) -> bool:
        # Readiness: only after the cache is synced and while not shutting down.
        return self.synced and not self.shutting_down

    def live(self, now: float) -> bool:
        # Liveness: the event loop must keep making progress.
        return (now - self.last_heartbeat) <= self.liveness_timeout

    def reconcile_safe(self, fn) -> str:
        try:
            return fn()
        except Exception:  # noqa: BLE001 - a panic must not kill the manager
            self.panics += 1
            return "requeue"


# ---------------------------------------------------------------------------
# SLO math
# ---------------------------------------------------------------------------
def error_budget_minutes(days: int = 30, slo: float = 0.999) -> float:
    return days * 24 * 60 * (1 - slo)


def burn_rate(observed_error_rate: float, slo: float = 0.999) -> float:
    return observed_error_rate / (1 - slo)


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------
def scenario_cache_sync() -> None:
    fast = InformerCache(objects=10_000, objects_per_second=2_000)
    assert fast.sync(timeout=120) == 5.0
    assert fast.synced

    slow = InformerCache(objects=300_000, objects_per_second=1_000)
    try:
        slow.sync(timeout=120)
    except CacheSyncTimeout as exc:
        assert "300.0s" in str(exc)
    else:  # pragma: no cover - the timeout is the contract
        raise AssertionError("a 300s sync must exceed a 120s timeout")
    manager = Manager(synced=slow.synced)
    assert not manager.ready()
    print("[cache] fast sync 5s -> Ready; 300k objects at 1k/s -> timeout, never Ready")


def scenario_leader_election() -> None:
    a, b, c = Candidate("operator-a"), Candidate("operator-b"), Candidate("operator-c")
    lease, leader = elect([a, b, c], None, now=0)
    assert leader is a
    assert lease and lease.expires_at == 15

    # a renews at t=10, so the lease extends to t=25.
    lease = a.renew(lease, now=10, duration=15)
    assert lease.expires_at == 25

    # a dies at t=12.  b/c cannot acquire until the lease expires at t=25.
    a.active = False
    lease, leader = elect([a, b, c], lease, now=24)
    assert leader is None and a.active is False  # lease still valid, nobody acting
    lease, leader = elect([a, b, c], lease, now=26)
    assert leader is b and b.active
    assert not a.active and not c.active
    # Fencing: at most one candidate is active at any point in this model.
    assert sum(candidate.active for candidate in (a, b, c)) == 1
    print("[leader] lease expiry at t=25, handover to b at t=26; exactly one active leader")


def scenario_workqueue() -> None:
    queue = WorkQueue(base_delay=0.005, max_retries=5)
    queue.add("raycluster/demo", now=0)
    item = queue.get(now=0)
    assert item and item.key == "raycluster/demo"
    delays = []
    for attempt in range(6):
        delay = queue.retry(item, now=10)
        delays.append(delay)
        if delay is None:
            break
        item = queue.get(now=10 + delay)
        assert item is not None
    # 5 retries, then the item is dropped instead of spinning forever.
    assert delays[:5] == [0.005, 0.01, 0.02, 0.04, 0.08]
    assert delays[5] is None
    assert queue.dropped == ["raycluster/demo"]
    assert queue.depth() == 0
    print(f"[queue] exponential backoff={delays[:5]}, then dropped after 5 retries")


def scenario_panic_recovery() -> None:
    manager = Manager()
    processed: list[str] = []

    def good(key: str):
        def inner() -> str:
            processed.append(key)
            return "done"

        return inner

    def bad() -> str:
        raise RuntimeError("nil pointer in reconciler")

    assert manager.reconcile_safe(good("a")) == "done"
    assert manager.reconcile_safe(bad) == "requeue"
    assert manager.reconcile_safe(good("b")) == "done"
    assert manager.panics == 1
    assert processed == ["a", "b"]
    print("[panic] one reconciler panicked; manager recovered and kept processing other keys")


def scenario_probes() -> None:
    manager = Manager(synced=False, last_heartbeat=0)
    assert not manager.ready()
    manager.synced = True
    assert manager.ready()
    # A blocked event loop fails liveness; the kubelet restarts the container.
    assert not manager.live(now=31)
    # Readiness can fail independently without restarting the process.
    manager.shutting_down = True
    assert not manager.ready()
    assert manager.live(now=1)
    print("[probes] readiness gates on cache sync; liveness fails only when the loop stalls")


def scenario_slo() -> None:
    budget = error_budget_minutes(days=30, slo=0.999)
    assert round(budget, 1) == 43.2
    assert round(burn_rate(0.0012), 2) == 1.2
    assert round(burn_rate(0.0005), 2) == 0.5
    print(f"[slo] 99.9% 30d budget={budget:.1f} min; 0.12% errors -> burn 1.2x")


def main() -> int:
    scenario_cache_sync()
    scenario_leader_election()
    scenario_workqueue()
    scenario_panic_recovery()
    scenario_probes()
    scenario_slo()
    print(
        "OK: cache sync, leader election, retry backoff, panic recovery, "
        "probe semantics and the reconcile SLO are reproducible."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
