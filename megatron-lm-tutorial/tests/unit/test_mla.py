"""Numerical tests for the standalone MLA reference implementation.

The goal is to test the *contract* that a production MLA kernel must preserve:

* causality: a future token cannot change an earlier output;
* cache equivalence: prefill + incremental decode equals a full forward;
* both query paths (direct and low-rank) train;
* the cached tensors really are the compressed latent + RoPE key (not K/V).

These tests are single-process; TP/CP sharding of MLA is discussed in chapter
30 but is intentionally not implemented in this teaching module.
"""
from __future__ import annotations

import torch

from mini_megatron.parallel.mla import MLAttention


def _module(**overrides) -> MLAttention:
    kwargs = dict(
        hidden_size=32,
        num_heads=4,
        q_lora_rank=16,
        kv_lora_rank=8,
        qk_nope_head_dim=8,
        qk_rope_head_dim=4,
        v_head_dim=8,
        max_seq_len=32,
    )
    kwargs.update(overrides)
    torch.manual_seed(1234)
    return MLAttention(**kwargs).eval()


def _positions(b: int, s: int, start: int = 0) -> torch.Tensor:
    return torch.arange(start, start + s).unsqueeze(0).expand(b, -1)


def test_mla_shapes_and_cache_layout():
    m = _module()
    x = torch.randn(2, 7, 32)
    out, cache = m(x, position_ids=_positions(2, 7), use_cache=True)
    assert out.shape == (2, 7, 32)
    c_kv, k_pe = cache
    # The cache is compressed: latent + one shared RoPE key, not full K/V.
    assert c_kv.shape == (2, 7, 8)
    assert k_pe.shape == (2, 1, 7, 4)
    assert c_kv.shape[-1] + k_pe.shape[-1] < 2 * 4 * 8


def test_mla_is_causal():
    m = _module()
    x1 = torch.randn(1, 10, 32)
    x2 = x1.clone()
    # Change only positions >= 4.
    x2[:, 4:] = torch.randn_like(x2[:, 4:])
    y1 = m(x1, position_ids=_positions(1, 10))
    y2 = m(x2, position_ids=_positions(1, 10))
    # Earlier outputs must not depend on the changed suffix.  The tolerance is
    # for fused-kernel reassociation; on CPU this is normally bit-identical.
    assert torch.allclose(y1[:, :4], y2[:, :4], atol=1e-6)
    assert not torch.allclose(y1[:, 4:], y2[:, 4:])


def test_mla_kv_cache_matches_full_forward():
    m = _module()
    x = torch.randn(2, 12, 32)
    full = m(x, position_ids=_positions(2, 12))

    prefill = 5
    pref, cache = m(
        x[:, :prefill],
        position_ids=_positions(2, prefill),
        use_cache=True,
    )
    pieces = [pref]
    for t in range(prefill, 12):
        step, cache = m(
            x[:, t : t + 1],
            position_ids=torch.full((2, 1), t, dtype=torch.long),
            past_key_value=cache,
            use_cache=True,
        )
        pieces.append(step)
    incremental = torch.cat(pieces, dim=1)
    assert torch.allclose(full, incremental, atol=1e-5, rtol=1e-5)


def test_mla_direct_query_path_and_gradients():
    m = _module(q_lora_rank=None)
    m.train()
    x = torch.randn(1, 6, 32, requires_grad=True)
    out = m(x, position_ids=_positions(1, 6))
    out.square().mean().backward()
    assert x.grad is not None and torch.isfinite(x.grad).all()
    assert m.q_proj is not None and m.q_proj.weight.grad is not None
    assert m.kv_a_proj_with_mqa.weight.grad is not None
    assert m.kv_b_proj.weight.grad is not None
    assert m.o_proj.weight.grad is not None


def test_mla_padding_mask_is_respected():
    m = _module()
    x = torch.randn(1, 6, 32)
    # Position 2 is padding.  Positions 3..5 would otherwise attend to it.
    mask = torch.tensor([[1, 1, 0, 1, 1, 1]])
    y = m(x, position_ids=_positions(1, 6), attention_mask=mask)
    x2 = x.clone()
    x2[:, 2] = torch.randn_like(x2[:, 2])
    y2 = m(x2, position_ids=_positions(1, 6), attention_mask=mask)
    assert torch.allclose(y[:, 3:], y2[:, 3:], atol=1e-6)


def test_mla_fully_masked_row_is_finite():
    m = _module()
    x = torch.randn(1, 5, 32, requires_grad=True)
    mask = torch.zeros(1, 5)
    out = m(
        x,
        position_ids=_positions(1, 5),
        attention_mask=mask,
    )
    assert torch.isfinite(out).all()
    out.sum().backward()
    assert x.grad is not None and torch.isfinite(x.grad).all()
