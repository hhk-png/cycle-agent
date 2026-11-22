"""Ring attention as a single-process reference implementation.

Ring attention is a context-parallel attention algorithm.  Each rank owns one
query block and one key/value block.  The KV blocks rotate around a ring; every
rank accumulates attention against one KV block at a time using an online
softmax.  No rank ever materialises the full ``s x s`` score matrix, and the
KV activation stays at ``1/cp``.

This module simulates all ``cp`` ranks in one process.  It is therefore a
*correctness* and *scheduling* reference, not a communication implementation:
the real system overlaps the P2P send/recv of the next KV block with the
current block's attention.  The arithmetic, causal masking and online-softmax
update are exactly what a kernel must preserve.

Causal load balance
-------------------
With a naive contiguous split, rank ``r`` processes ``r+1`` KV blocks (it can
skip all future blocks), so the last rank does ``cp`` times the work of the
first.  Production CP uses a load-balanced causal mask: split the query range
in half and pair early queries with late keys, so every rank sees the same
number of diagonal blocks.  :func:`ring_attention_plan` reports the naive
imbalance; the reference below keeps the straightforward contiguous layout so
the masking is easy to audit.
"""
from __future__ import annotations

import math
from typing import Dict, Tuple

import torch


def _split_heads(x: torch.Tensor, cp_size: int, block: int, r: int) -> torch.Tensor:
    return x[:, :, r * block : (r + 1) * block, :]


def ring_attention(
    q: torch.Tensor,
    k: torch.Tensor,
    v: torch.Tensor,
    *,
    cp_size: int,
    causal: bool = True,
    return_stats: bool = False,
):
    """Compute attention as ``cp_size`` simulated ring ranks.

    Parameters
    ----------
    q, k, v:
        ``[batch, heads, seq, head_dim]``.  ``seq`` must be divisible by
        ``cp_size``.  ``k`` and ``v`` must have the same sequence length as
        ``q`` (this reference does not implement GQA repetition; expand KV
        heads before calling it).
    cp_size:
        Number of simulated context-parallel ranks.
    causal:
        Apply the global causal mask.  With ``causal=False`` every rank reads
        every KV block.
    return_stats:
        Also return a dict with per-rank block counts and the imbalance ratio.

    Returns
    -------
    out:
        ``[batch, heads, seq, v_head_dim]``.
    stats:
        Only when ``return_stats=True``.
    """
    if q.dim() != 4 or k.dim() != 4 or v.dim() != 4:
        raise ValueError("q, k, v must be [batch, heads, seq, head_dim]")
    if q.shape[0] != k.shape[0] or q.shape[1] != k.shape[1]:
        raise ValueError("batch/head dimensions must match")
    seq = q.shape[2]
    if seq % cp_size != 0:
        raise ValueError("sequence length must be divisible by cp_size")
    if k.shape[2] != seq:
        raise ValueError("this reference requires q and kv sequence lengths to match")

    block = seq // cp_size
    scale = 1.0 / math.sqrt(q.shape[-1])
    outputs = []
    counts = []

    for r in range(cp_size):
        q_r = _split_heads(q, cp_size, block, r)
        # Running online-softmax state.
        m_i = torch.full(
            (q.shape[0], q.shape[1], block),
            float("-inf"),
            dtype=q.dtype,
            device=q.device,
        )
        l_i = torch.zeros_like(m_i)
        acc = torch.zeros(
            q.shape[0],
            q.shape[1],
            block,
            v.shape[-1],
            dtype=q.dtype,
            device=q.device,
        )
        processed = 0
        # The KV block index walks backwards around the ring.  Rank r starts
        # with its own block, then receives r-1, r-2, ...
        for step in range(cp_size):
            j = (r - step) % cp_size
            if causal and j > r:
                # Entire block is in the future for every local query.
                continue
            k_j = _split_heads(k, cp_size, block, j)
            v_j = _split_heads(v, cp_size, block, j)
            scores = torch.matmul(q_r, k_j.transpose(-2, -1)) * scale

            if causal:
                q_pos = torch.arange(r * block, (r + 1) * block, device=q.device)
                kv_pos = torch.arange(j * block, (j + 1) * block, device=q.device)
                mask = kv_pos.unsqueeze(0) <= q_pos.unsqueeze(1)
                scores = scores.masked_fill(~mask, float("-inf"))

            block_max = scores.max(dim=-1).values
            m_new = torch.maximum(m_i, block_max)
            # exp(-inf - finite) is 0; the first valid block therefore
            # contributes with alpha=0 and does not poison the accumulator.
            alpha = torch.exp(m_i - m_new)
            p = torch.exp(scores - m_new.unsqueeze(-1))
            l_i = l_i * alpha + p.sum(dim=-1)
            acc = acc * alpha.unsqueeze(-1) + torch.matmul(p, v_j)
            m_i = m_new
            processed += 1

        outputs.append(acc / l_i.unsqueeze(-1))
        counts.append(processed)

    out = torch.cat(outputs, dim=2)
    if not return_stats:
        return out
    stats = {
        "cp_size": cp_size,
        "blocks_per_rank": counts,
        "max_blocks": max(counts),
        "min_blocks": min(counts),
        "imbalance": max(counts) / max(1, min(counts)),
    }
    return out, stats


def dense_causal_attention(q: torch.Tensor, k: torch.Tensor, v: torch.Tensor) -> torch.Tensor:
    """Reference implementation used by the tests and by the tutorial text."""
    scale = 1.0 / math.sqrt(q.shape[-1])
    scores = torch.matmul(q, k.transpose(-2, -1)) * scale
    seq = q.shape[2]
    mask = torch.ones(seq, seq, dtype=torch.bool, device=q.device).tril()
    scores = scores.masked_fill(~mask, float("-inf"))
    probs = torch.softmax(scores, dim=-1)
    return torch.matmul(probs, v)


def ring_attention_plan(cp_size: int, causal: bool = True) -> Dict[str, object]:
    """Static block schedule, useful for capacity planning and exercises."""
    if cp_size < 1:
        raise ValueError("cp_size must be positive")
    if causal:
        counts = [r + 1 for r in range(cp_size)]
    else:
        counts = [cp_size] * cp_size
    total = sum(counts)
    balanced = cp_size * cp_size if not causal else cp_size * (cp_size + 1) / 2
    return {
        "cp_size": cp_size,
        "causal": causal,
        "blocks_per_rank": counts,
        "total_block_passes": total,
        "average_blocks": total / cp_size,
        "imbalance": max(counts) / max(1, min(counts)),
        "dense_block_passes": balanced,
    }
