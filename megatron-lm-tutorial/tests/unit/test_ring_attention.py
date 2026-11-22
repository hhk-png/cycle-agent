"""Ring attention reference: numerics, causality, schedule and gradients."""
from __future__ import annotations

import torch

from mini_megatron.parallel.ring_attention import (
    dense_causal_attention,
    ring_attention,
    ring_attention_plan,
)


def _tensors(seq: int = 16, heads: int = 3, dim: int = 8, seed: int = 1):
    g = torch.Generator().manual_seed(seed)
    q = torch.randn(2, heads, seq, dim, generator=g)
    k = torch.randn(2, heads, seq, dim, generator=g)
    v = torch.randn(2, heads, seq, dim, generator=g)
    return q, k, v


def test_ring_matches_dense_causal_for_multiple_cp_sizes():
    for cp in (1, 2, 4):
        q, k, v = _tensors(seed=cp)
        out = ring_attention(q, k, v, cp_size=cp)
        ref = dense_causal_attention(q, k, v)
        assert torch.allclose(out, ref, atol=1e-5, rtol=1e-5), cp


def test_ring_matches_dense_noncausal():
    q, k, v = _tensors(seed=9)
    out = ring_attention(q, k, v, cp_size=2, causal=False)
    scale = 1.0 / (q.shape[-1] ** 0.5)
    ref = torch.softmax(torch.matmul(q, k.transpose(-2, -1)) * scale, dim=-1)
    ref = torch.matmul(ref, v)
    assert torch.allclose(out, ref, atol=1e-5, rtol=1e-5)


def test_ring_is_causal_and_stable():
    q, k, v = _tensors(seed=3)
    out = ring_attention(q, k, v, cp_size=4)
    q2, k2, v2 = q.clone(), k.clone(), v.clone()
    # Change only the last quarter; earlier outputs must not move.
    q2[:, :, 12:] = torch.randn_like(q2[:, :, 12:]) * 10
    k2[:, :, 12:] = torch.randn_like(k2[:, :, 12:]) * 10
    v2[:, :, 12:] = torch.randn_like(v2[:, :, 12:]) * 10
    out2 = ring_attention(q2, k2, v2, cp_size=4)
    assert torch.allclose(out[:, :, :12], out2[:, :, :12], atol=1e-5)
    assert not torch.allclose(out[:, :, 12:], out2[:, :, 12:])

    # Large scores would overflow a naive exp; the online max must keep it
    # finite and equal to the dense reference.
    qs = q * 40
    ks = k * 40
    stable = ring_attention(qs, ks, v, cp_size=4)
    assert torch.isfinite(stable).all()
    assert torch.allclose(stable, dense_causal_attention(qs, ks, v), atol=1e-4)


def test_ring_gradients_match_dense():
    q, k, v = _tensors(seq=12, seed=5)
    q1 = q.clone().requires_grad_(True)
    k1 = k.clone().requires_grad_(True)
    v1 = v.clone().requires_grad_(True)
    ring_attention(q1, k1, v1, cp_size=3).square().mean().backward()

    q2 = q.clone().requires_grad_(True)
    k2 = k.clone().requires_grad_(True)
    v2 = v.clone().requires_grad_(True)
    dense_causal_attention(q2, k2, v2).square().mean().backward()

    for a, b in ((q1, q2), (k1, k2), (v1, v2)):
        assert torch.allclose(a.grad, b.grad, atol=2e-5, rtol=2e-5)


def test_ring_plan_reports_load_imbalance():
    plan = ring_attention_plan(4)
    assert plan["blocks_per_rank"] == [1, 2, 3, 4]
    assert plan["imbalance"] == 4.0
    balanced = ring_attention_plan(4, causal=False)
    assert balanced["blocks_per_rank"] == [4, 4, 4, 4]
    assert balanced["imbalance"] == 1.0

    _, stats = ring_attention(*_tensors(seed=2), cp_size=4, return_stats=True)
    assert stats["blocks_per_rank"] == [1, 2, 3, 4]
