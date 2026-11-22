"""Ulysses reference: layout equivalence, causality, gradients and stats."""
from __future__ import annotations

import pytest
import torch

from mini_megatron.parallel.ulysses import (
    dense_causal_attention,
    ulysses_attention,
    ulysses_layout_roundtrip,
)


def _tensors(seq: int = 16, heads: int = 4, dim: int = 8, seed: int = 1):
    g = torch.Generator().manual_seed(seed)
    q = torch.randn(2, heads, seq, dim, generator=g)
    k = torch.randn(2, heads, seq, dim, generator=g)
    v = torch.randn(2, heads, seq, dim, generator=g)
    return q, k, v


def test_ulysses_matches_dense_causal_for_multiple_cp_sizes():
    for cp in (1, 2, 4):
        q, k, v = _tensors(seed=cp)
        out = ulysses_attention(q, k, v, cp_size=cp)
        ref = dense_causal_attention(q, k, v)
        assert torch.allclose(out, ref, atol=1e-5, rtol=1e-5), cp


def test_ulysses_matches_dense_noncausal():
    q, k, v = _tensors(seed=9)
    out = ulysses_attention(q, k, v, cp_size=2, causal=False)
    scale = 1.0 / (q.shape[-1] ** 0.5)
    probs = torch.softmax(torch.matmul(q, k.transpose(-2, -1)) * scale, dim=-1)
    assert torch.allclose(out, torch.matmul(probs, v), atol=1e-5, rtol=1e-5)


def test_ulysses_is_causal():
    """Changing future tokens must not move earlier outputs.

    This catches an inverted causal mask.  A dense reference with the same
    inversion would agree with the buggy implementation, so this test mutates
    the input instead of comparing to another mask.
    """
    q, k, v = _tensors(seq=16, heads=4, seed=3)
    out = ulysses_attention(q, k, v, cp_size=4)
    q2, k2, v2 = q.clone(), k.clone(), v.clone()
    q2[:, :, 12:] = torch.randn_like(q2[:, :, 12:]) * 10
    k2[:, :, 12:] = torch.randn_like(k2[:, :, 12:]) * 10
    v2[:, :, 12:] = torch.randn_like(v2[:, :, 12:]) * 10
    out2 = ulysses_attention(q2, k2, v2, cp_size=4)
    assert torch.allclose(out[:, :, :12], out2[:, :, :12], atol=1e-5)
    assert not torch.allclose(out[:, :, 12:], out2[:, :, 12:])

    # Non-causal attention *must* see the future; otherwise the mask flag is
    # being ignored.
    noncausal = ulysses_attention(q2, k2, v2, cp_size=4, causal=False)
    baseline = ulysses_attention(q, k, v, cp_size=4, causal=False)
    assert not torch.allclose(noncausal[:, :, :12], baseline[:, :, :12])


def test_ulysses_layout_roundtrip_is_exact():
    x = torch.randn(2, 8, 12, 5)
    for cp in (1, 2, 4):
        rebuilt, exact = ulysses_layout_roundtrip(x, cp_size=cp)
        assert exact
        assert torch.equal(rebuilt, x)


def test_ulysses_rejects_indivisible_layouts():
    q, k, v = _tensors(seq=10, heads=6)
    with pytest.raises(ValueError, match="sequence"):
        ulysses_attention(q, k, v, cp_size=4)
    q, k, v = _tensors(seq=16, heads=6)
    with pytest.raises(ValueError, match="head"):
        ulysses_attention(q, k, v, cp_size=4)
    with pytest.raises(ValueError, match="positive"):
        ulysses_attention(q, k, v, cp_size=0)


def test_ulysses_gradients_match_dense():
    q, k, v = _tensors(seq=12, heads=4, seed=5)
    q1 = q.clone().requires_grad_(True)
    k1 = k.clone().requires_grad_(True)
    v1 = v.clone().requires_grad_(True)
    ulysses_attention(q1, k1, v1, cp_size=2).square().mean().backward()

    q2 = q.clone().requires_grad_(True)
    k2 = k.clone().requires_grad_(True)
    v2 = v.clone().requires_grad_(True)
    dense_causal_attention(q2, k2, v2).square().mean().backward()

    for a, b in ((q1, q2), (k1, k2), (v1, v2)):
        assert torch.allclose(a.grad, b.grad, atol=2e-5, rtol=2e-5)


def test_ulysses_communication_stats():
    q, k, v = _tensors(seq=16, heads=4, dim=8)
    _, stats = ulysses_attention(q, k, v, cp_size=2, return_stats=True)
    # 4 tensors * B*H*(S/P)*D * (P-1)/P, with B=2,H=4,S/P=8,D=8,P=2.
    local = 2 * 4 * 8 * 8
    assert stats["send_elements_per_rank"] == 4 * local // 2
    assert stats["total_elements"] == 4 * local
    # Aggregate traffic is 4*B*S*H*D*(P-1)/P, i.e. 2/P of ring's
    # 2*B*S*H*D*(P-1) at the same P (equal at P=2, lower beyond); the price is
    # two all-to-all latencies per layer.
    assert stats["total_elements"] == 4 * 2 * 16 * 4 * 8 * (2 - 1) // 2
