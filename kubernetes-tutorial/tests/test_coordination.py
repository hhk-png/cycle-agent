from __future__ import annotations

from mini_k8s.coordination.leader import LeaderElector
from mini_k8s.store.store import ResourceStore


def make_electors():
    store = ResourceStore()
    clock = [1_700_000_000.0]

    def now() -> float:
        return clock[0]

    first = LeaderElector(store, "candidate-a", lease_name="controller", namespace="kube-system",
                          lease_duration=10, clock=now)
    second = LeaderElector(store, "candidate-b", lease_name="controller", namespace="kube-system",
                           lease_duration=10, clock=now)
    return store, clock, first, second


def test_single_leader_and_renewal():
    store, clock, first, second = make_electors()
    assert first.try_acquire_or_renew()
    assert first.is_leader()
    assert not second.try_acquire_or_renew()
    clock[0] += 5
    assert first.try_acquire_or_renew()
    assert first.is_leader()
    lease = store.get(("coordination.k8s.io", "v1", "leases"), "kube-system", "controller")
    assert lease["spec"]["holderIdentity"] == "candidate-a"


def test_failover_after_expiry_and_transition():
    store, clock, first, second = make_electors()
    assert first.try_acquire_or_renew()
    clock[0] += 11
    assert not first.is_leader()
    assert second.try_acquire_or_renew()
    assert second.is_leader()
    lease = store.get(("coordination.k8s.io", "v1", "leases"), "kube-system", "controller")
    assert lease["spec"]["holderIdentity"] == "candidate-b"
    assert lease["spec"]["leaseTransitions"] == 1


def test_release_allows_immediate_takeover():
    store, clock, first, second = make_electors()
    assert first.try_acquire_or_renew()
    first.release()
    assert second.try_acquire_or_renew()
    assert second.is_leader()
