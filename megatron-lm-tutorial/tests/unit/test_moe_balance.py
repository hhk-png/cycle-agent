"""Aux-loss-free balancing and node-limited routing."""
from __future__ import annotations

import torch

from mini_megatron.parallel.moe_balance import (
    AuxFreeBalancer,
    global_expert_counts,
    node_limited_topk,
)


def test_bias_moves_against_the_load_direction():
    balancer = AuxFreeBalancer(4, update_speed=0.01, normalize=False)
    counts = torch.tensor([100.0, 0.0, 0.0, 0.0])
    balancer.update(counts)
    assert balancer.bias[0] < 0
    assert torch.all(balancer.bias[1:] > 0)
    # A balanced histogram leaves the bias untouched.
    before = balancer.bias.clone()
    balancer.update(torch.full((4,), 25.0))
    assert torch.equal(before, balancer.bias)


def test_bias_changes_selection_not_weights():
    torch.manual_seed(0)
    balancer = AuxFreeBalancer(4, update_speed=0.05, normalize=False)
    # Expert 0 dominates by a small margin.
    scores = torch.tensor(
        [[0.50, 0.49, 0.48, 0.47], [0.50, 0.49, 0.48, 0.47]]
    )
    weights0, idx0 = balancer.select(scores, 1)
    assert torch.all(idx0 == 0)
    assert torch.allclose(weights0, scores[:, :1])

    for _ in range(5):
        balancer.update(torch.tensor([2.0, 0.0, 0.0, 0.0]))
    _, idx1 = balancer.select(scores, 1)
    assert not torch.all(idx1 == 0)
    # The selected weight is still the *unbiased* affinity score.
    selected = scores.gather(-1, idx1)
    weights1, _ = balancer.select(scores, 1)
    assert torch.allclose(weights1, selected)


def test_balancer_state_roundtrip():
    a = AuxFreeBalancer(3, update_speed=0.02, normalize=True)
    a.update(torch.tensor([5.0, 1.0, 0.0]))
    b = AuxFreeBalancer(3, update_speed=0.9, normalize=False)
    b.load_state_dict(a.state_dict())
    assert torch.equal(a.bias, b.bias)
    assert a.updates == b.updates
    # Hyperparameters are part of the state: a resumed balancer must move at
    # the saved speed and normalise the same way.
    assert b.update_speed == a.update_speed
    assert b.normalize is a.normalize


def test_global_counts_is_a_noop_without_a_group():
    counts = torch.tensor([1.0, 2.0])
    assert torch.equal(global_expert_counts(counts), counts)


def test_node_limited_topk_obeys_node_budget():
    torch.manual_seed(3)
    num_experts, epn, k = 8, 2, 2
    scores = torch.rand(32, num_experts)
    # Force the global top-k to span several nodes.
    scores[:, [0, 2, 4, 6]] += 1.0
    _, idx = node_limited_topk(
        scores, k, experts_per_node=epn, max_nodes=1
    )
    nodes = idx // epn
    assert all(nodes[i].unique().numel() <= 1 for i in range(scores.size(0)))
    # With max_nodes=2 the budget relaxes but still holds.
    _, idx2 = node_limited_topk(
        scores, k, experts_per_node=epn, max_nodes=2
    )
    nodes2 = idx2 // epn
    assert all(nodes2[i].unique().numel() <= 2 for i in range(scores.size(0)))
    # Determinism.
    _, again = node_limited_topk(
        scores, k, experts_per_node=epn, max_nodes=2
    )
    assert torch.equal(idx2, again)


def test_node_limited_topk_rejects_impossible_budget():
    scores = torch.rand(2, 8)
    try:
        node_limited_topk(scores, 3, experts_per_node=2, max_nodes=1)
    except ValueError:
        pass
    else:
        raise AssertionError("expected an impossible node budget to raise")
