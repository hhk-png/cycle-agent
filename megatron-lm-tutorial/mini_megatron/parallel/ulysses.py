"""Ulysses context parallelism as a single-process reference implementation.

Ulysses (DeepSpeed) and ring attention solve the same problem with opposite
layout choices:

* **Ring**: keep the head dimension intact, rotate K/V sequence blocks, and
  never materialise the full ``s x s`` score matrix.
* **Ulysses**: exchange layout with two all-to-all calls.  After the first
  all-to-all every rank owns **all** tokens for a slice of heads, so attention
  is an ordinary full-sequence attention for those heads; the second
  all-to-all restores the sequence-sharded layout for the output projection
  and MLP.

This module simulates all ``cp`` ranks in one process, exactly like
:mod:`mini_megatron.parallel.ring_attention`.  It is an arithmetic and layout
reference, not a communication implementation.  The real system pays two
all-to-all latencies per layer and needs a topology-friendly head/sequence
split; the reference lets us check the permutation, the global causal mask and
the gradients against dense attention.

Layouts
-------
Let ``P = cp_size``, ``S`` the sequence length and ``H`` the head count.

``before`` (per rank): ``[B, H, S/P, D]`` -- one sequence chunk, all heads.
``after``  (per rank): ``[B, H/P, S, D]`` -- all tokens, one head slice.

The two transformations are transposes of each other; applying both is the
identity.  The function below fuses the permutation with the attention
computation so the reference stays compact.

Limitations
-----------
* ``S`` and ``H`` must both be divisible by ``P``.
* K/V must have the same head count as Q.  For GQA, repeat K/V heads before
  calling (a real Ulysses implementation splits KV heads, so ``H_kv`` must be
  divisible by ``P`` or KV must be replicated -- a key practical constraint
  when the KV head count is small, e.g. 8).
* The attention matrix is materialised; a production kernel would use
  FlashAttention on the head-sliced layout.
"""
from __future__ import annotations

import math
from typing import Dict, Tuple

import torch


def ulysses_attention(
    q: torch.Tensor,
    k: torch.Tensor,
    v: torch.Tensor,
    *,
    cp_size: int,
    causal: bool = True,
    return_stats: bool = False,
):
    """Compute attention as ``cp_size`` simulated Ulysses ranks.

    Parameters
    ----------
    q, k, v:
        ``[batch, heads, seq, head_dim]`` in the **global** layout (the layout
        a single GPU would use).  ``seq`` and ``heads`` must be divisible by
        ``cp_size``.
    cp_size:
        Number of simulated context-parallel ranks.
    causal:
        Apply the global causal mask.
    return_stats:
        Also return per-rank and aggregate all-to-all element counts.

    Returns
    -------
    out:
        ``[batch, heads, seq, v_head_dim]``.
    stats:
        Only when ``return_stats=True``; keys ``cp_size``,
        ``send_elements_per_rank``, ``total_elements``.
    """
    if q.dim() != 4 or k.dim() != 4 or v.dim() != 4:
        raise ValueError("q, k, v must be [batch, heads, seq, head_dim]")
    if q.shape[0] != k.shape[0] or q.shape[1] != k.shape[1]:
        raise ValueError("batch/head dimensions must match")
    if cp_size < 1:
        raise ValueError("cp_size must be positive")
    batch, heads, seq, head_dim = q.shape
    if seq % cp_size != 0:
        raise ValueError("sequence length must be divisible by cp_size")
    if heads % cp_size != 0:
        raise ValueError("head count must be divisible by cp_size")
    if k.shape[2] != seq:
        raise ValueError("this reference requires q and kv sequence lengths to match")

    seq_block = seq // cp_size
    head_block = heads // cp_size
    scale = 1.0 / math.sqrt(head_dim)

    # First all-to-all: rank r's initial layout is [B, H, S/P, D] for sequence
    # chunk r.  After the exchange it owns head slice r over the full sequence.
    # We build each rank's view by concatenating the same head slice from every
    # sequence chunk.
    q_by_head = []
    k_by_head = []
    v_by_head = []
    for r in range(cp_size):
        h0 = r * head_block
        h1 = h0 + head_block
        q_by_head.append(
            torch.cat(
                [q[:, h0:h1, s0 : s0 + seq_block, :] for s0 in range(0, seq, seq_block)],
                dim=2,
            )
        )
        k_by_head.append(
            torch.cat(
                [k[:, h0:h1, s0 : s0 + seq_block, :] for s0 in range(0, seq, seq_block)],
                dim=2,
            )
        )
        v_by_head.append(
            torch.cat(
                [v[:, h0:h1, s0 : s0 + seq_block, :] for s0 in range(0, seq, seq_block)],
                dim=2,
            )
        )

    # Ordinary full-sequence attention, but each rank only owns H/P heads.
    # Positions are global, so the causal mask is identical on every rank.
    outputs = []
    for r in range(cp_size):
        scores = torch.matmul(q_by_head[r], k_by_head[r].transpose(-2, -1)) * scale
        if causal:
            # mask[i, j] = (key j <= query i).  Get the orientation wrong and
            # the model silently attends to the future -- and any test that
            # compares against a dense reference with the same mistake passes.
            q_pos = torch.arange(seq, device=q.device)
            mask = q_pos.unsqueeze(0) <= q_pos.unsqueeze(1)
            scores = scores.masked_fill(~mask, float("-inf"))
        probs = torch.softmax(scores, dim=-1)
        # A fully masked row cannot occur with causal=True (the diagonal is
        # always visible), but keep the same guard as the other references.
        probs = torch.nan_to_num(probs, nan=0.0)
        outputs.append(torch.matmul(probs, v_by_head[r]))

    # Second all-to-all: rank r_seq collects its own sequence chunk from every
    # head-owning rank and concatenates the head slices back in order.
    chunks = []
    for r_seq in range(cp_size):
        s0 = r_seq * seq_block
        chunks.append(
            torch.cat(
                [out[:, :, s0 : s0 + seq_block, :] for out in outputs],
                dim=1,
            )
        )
    out = torch.cat(chunks, dim=2)

    if not return_stats:
        return out
    # Q, K, V are exchanged in the first all-to-all; the attention output in
    # the second.  Each rank sends (P-1)/P of its local elements for each of
    # the four tensors.  Local tensor size before the first all-to-all is
    # B*H*(S/P)*D; after the second it is B*H*(S/P)*D as well.
    local_elements = batch * heads * seq_block * head_dim
    send_per_rank = 4 * local_elements * (cp_size - 1) // cp_size
    stats = {
        "cp_size": cp_size,
        "send_elements_per_rank": send_per_rank,
        "total_elements": send_per_rank * cp_size,
    }
    return out, stats


def dense_causal_attention(
    q: torch.Tensor, k: torch.Tensor, v: torch.Tensor
) -> torch.Tensor:
    """Single-GPU dense reference with the same global causal mask."""
    scale = 1.0 / math.sqrt(q.shape[-1])
    scores = torch.matmul(q, k.transpose(-2, -1)) * scale
    seq = q.shape[2]
    # mask[i, j] = (key j <= query i).
    mask = torch.arange(seq, device=q.device).unsqueeze(0) <= torch.arange(
        seq, device=q.device
    ).unsqueeze(1)
    scores = scores.masked_fill(~mask, float("-inf"))
    probs = torch.softmax(scores, dim=-1)
    probs = torch.nan_to_num(probs, nan=0.0)
    return torch.matmul(probs, v)


def ulysses_layout_roundtrip(
    x: torch.Tensor, *, cp_size: int
) -> Tuple[torch.Tensor, bool]:
    """Exercise the two layout transformations without attention.

    ``x`` is ``[batch, heads, seq, dim]``.  The function simulates the first
    all-to-all (sequence blocks -> head slices), then the inverse, and returns
    the reconstructed tensor plus whether the round trip was bit-identical.
    This is a cheap invariant test for a real implementation: if the two
    permutations are not exact inverses, attention will silently mix heads or
    positions.
    """
    if x.dim() != 4:
        raise ValueError("x must be [batch, heads, seq, dim]")
    batch, heads, seq, dim = x.shape
    if seq % cp_size or heads % cp_size:
        raise ValueError("seq and heads must be divisible by cp_size")
    seq_block, head_block = seq // cp_size, heads // cp_size

    sharded = []
    for r in range(cp_size):
        h0 = r * head_block
        sharded.append(
            torch.cat(
                [x[:, h0 : h0 + head_block, s0 : s0 + seq_block, :] for s0 in range(0, seq, seq_block)],
                dim=2,
            )
        )
    rebuilt = []
    for r_seq in range(cp_size):
        s0 = r_seq * seq_block
        rebuilt.append(
            torch.cat([t[:, :, s0 : s0 + seq_block, :] for t in sharded], dim=1)
        )
    out = torch.cat(rebuilt, dim=2)
    return out, bool(torch.equal(out, x))
