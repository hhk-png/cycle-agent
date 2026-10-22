from __future__ import annotations

import numpy as np
import pytest

from mini_deepinfer.eplb import imbalance, no_replication_plan, rank_loads, rebalance_experts


def test_eplb_balances_skewed_load() -> None:
    loads = np.array([1000, 800, 600, 100, 50, 20, 10, 5], dtype=float)
    plan = rebalance_experts(loads, num_physical_experts=12, num_local_physical_experts=6)
    baseline = no_replication_plan(8, 8, 4)
    assert imbalance(loads, plan) < imbalance(loads, baseline)
    assert imbalance(loads, plan) < 1.2
    assert len(plan.physical_to_logical) == 12
    assert plan.logical_count.sum() == 12
    assert len(plan.rank_experts) == 2
    assert all(len(experts) == 6 for experts in plan.rank_experts)


def test_eplb_maps_are_consistent() -> None:
    loads = np.array([10, 1, 1, 1], dtype=float)
    plan = rebalance_experts(loads, num_physical_experts=8, num_local_physical_experts=4)
    for physical, logical in enumerate(plan.physical_to_logical):
        assert physical in plan.logical_to_physical[logical]


def test_rank_loads_sum_to_total() -> None:
    loads = np.array([10, 4, 2, 1], dtype=float)
    plan = rebalance_experts(loads, num_physical_experts=4, num_local_physical_experts=2)
    assert np.isclose(rank_loads(loads, plan).sum(), loads.sum())


def test_eplb_rejects_invalid_capacity() -> None:
    with pytest.raises(ValueError):
        rebalance_experts(np.ones(4), 6, 4)
    with pytest.raises(ValueError):
        rebalance_experts(np.ones(4), 3, 1)
