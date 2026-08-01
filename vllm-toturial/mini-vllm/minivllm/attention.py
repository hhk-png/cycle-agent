"""Paged attention: attention over a non-contiguous block KV cache.

In real vLLM this is a fused Triton / CUDA kernel
(``vllm/attention/ops/paged_attn.py``).  The mini version keeps the exact same
*data layout* (``(num_blocks, block_size, num_heads, head_dim)`` plus a block
table) but implements the math in pure NumPy so every step is easy to read.
"""

from __future__ import annotations

from typing import List

import numpy as np

from .kv_cache import KVStore


def _softmax(x: np.ndarray, axis: int = -1) -> np.ndarray:
    x = x - np.max(x, axis=axis, keepdims=True)
    e = np.exp(x)
    return e / np.sum(e, axis=axis, keepdims=True)


def gather_kv(k_cache: np.ndarray,
              v_cache: np.ndarray,
              block_table: List[int],
              logical_start: int,
              logical_end: int,
              block_size: int,
              num_heads: int,
              head_dim: int):
    """Copy the K/V of logical positions ``[logical_start, logical_end)``.

    The positions may be spread across several *non-contiguous* physical
    blocks; the block table is what glues them together, exactly like a page
    table in an operating system.

    Returns arrays of shape ``(num_positions, num_heads, head_dim)``.
    """
    num_positions = logical_end - logical_start
    ks: List[np.ndarray] = []
    vs: List[np.ndarray] = []
    block_start = logical_start // block_size
    block_end = (logical_end + block_size - 1) // block_size
    for bi in range(block_start, block_end):
        phys = block_table[bi]
        lo = max(0, logical_start - bi * block_size)
        hi = min(block_size, logical_end - bi * block_size)
        ks.append(k_cache[phys, lo:hi])
        vs.append(v_cache[phys, lo:hi])
    k = np.concatenate(ks, axis=0)
    v = np.concatenate(vs, axis=0)
    assert k.shape[0] == num_positions, (k.shape, num_positions)
    return k, v


def paged_attention_batch(
    q: np.ndarray,            # (B, num_heads, num_new_tokens, head_dim)
    k: np.ndarray,            # (B, num_heads, num_new_tokens, head_dim)
    v: np.ndarray,            # (B, num_heads, num_new_tokens, head_dim)
    cached_lens: np.ndarray,  # (B,)  how many tokens are already in the KV cache
    block_tables: List[List[int]],
    kv_store: KVStore,
    layer: int,
    num_heads: int,
    head_dim: int,
    block_size: int,
) -> np.ndarray:
    """Run causal paged attention for a batch of sequences.

    Each sequence has ``cached_len`` tokens already stored in its physical
    blocks and ``num_new_tokens`` tokens being processed in this step.  The
    new tokens attend to *all* previous tokens (cached + new), which is the
    one step that differs between the prefill phase (many new tokens) and the
    decode phase (exactly one new token).
    """
    B, _, num_new, _ = q.shape
    outs = []
    k_cache = kv_store.k_cache[layer]
    v_cache = kv_store.v_cache[layer]

    for b in range(B):
        qb = q[b]                       # (num_heads, num_new, head_dim)
        kb = k[b].transpose(1, 0, 2)    # (num_new, num_heads, head_dim)
        vb = v[b].transpose(1, 0, 2)
        cached_len = int(cached_lens[b])
        total_len = cached_len + num_new

        if cached_len > 0:
            # gather the cached prefix from scattered physical blocks
            k_cached, v_cached = gather_kv(
                k_cache, v_cache, block_tables[b], 0, cached_len,
                block_size, num_heads, head_dim)
            k_full = np.concatenate([k_cached, kb], axis=0)  # (total_len, H, D)
            v_full = np.concatenate([v_cached, vb], axis=0)
        else:
            k_full = kb
            v_full = vb

        # scores[head, i, j] = q_i . k_j / sqrt(head_dim)
        scores = np.einsum("hit,jht->hij", qb, k_full) / np.sqrt(head_dim)
        # causal mask: query row i (of the new tokens) attends to keys
        # positions 0 .. cached_len + i (inclusive)
        key_positions = np.arange(total_len)[None, :]
        query_positions = cached_len + np.arange(num_new)[:, None]
        mask = key_positions > query_positions
        scores = np.where(mask, -np.inf, scores)
        probs = _softmax(scores, axis=-1)
        out = np.einsum("hij,jht->hit", probs, v_full)
        outs.append(out)

    return np.stack(outs, axis=0)  # (B, num_heads, num_new, head_dim)
