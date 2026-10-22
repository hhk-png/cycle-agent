"""A block-sparse attention mechanism lab, inspired by CSA2's indexer.

The indexer scores blocks, the top-k blocks are selected per query, and the
main attention is masked to those blocks.  With ``top_k >= num_blocks`` this
must be numerically identical to dense causal attention.
"""

from __future__ import annotations

import numpy as np

from .model.ops import attention, causal_mask, expand_kv


def block_sparse_attention(
    q: np.ndarray,
    k: np.ndarray,
    v: np.ndarray,
    *,
    block_size: int,
    top_k: int,
    indexer_scores: np.ndarray | None = None,
    window: int | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    """Compute attention over the top-k key blocks for each query.

    q: ``[Q, H, D]``; k/v: ``[T, Hkv, D]``.  Returns the context and the
    selected block ids ``[Q, top_k]``.
    """
    if block_size < 1 or top_k < 1:
        raise ValueError("block_size and top_k must be positive")
    q = np.asarray(q, dtype=np.float32)
    k = np.asarray(k, dtype=np.float32)
    v = np.asarray(v, dtype=np.float32)
    num_blocks = (k.shape[0] + block_size - 1) // block_size
    top_k = min(top_k, num_blocks)
    if indexer_scores is None:
        # A cheap content score: mean query attends to mean key per block.
        q_mean = q.mean(axis=0)  # [H, D]
        k_heads = expand_kv(k, v, q.shape[1])[0]
        scores = np.zeros((q.shape[0], num_blocks), dtype=np.float32)
        for block in range(num_blocks):
            start = block * block_size
            end = min(start + block_size, k.shape[0])
            block_mean = k_heads[start:end].mean(axis=0)  # [H, D]
            scores[:, block] = np.einsum("hd,hd->", q_mean, block_mean)
    else:
        scores = np.asarray(indexer_scores, dtype=np.float32)
        if scores.shape != (q.shape[0], num_blocks):
            raise ValueError("indexer_scores must be [Q, num_blocks]")

    # The indexer must not select a block whose first key is in the future;
    # such a block has no causal key for this query and would make softmax NaN.
    scores = np.array(scores, copy=True)
    for query in range(q.shape[0]):
        for block in range(num_blocks):
            if block * block_size > query:
                scores[query, block] = -np.inf
    selected = np.argsort(-scores, axis=1)[:, :top_k]
    selected_mask = np.zeros((q.shape[0], k.shape[0]), dtype=bool)
    for query in range(q.shape[0]):
        for block in selected[query]:
            start = int(block) * block_size
            selected_mask[query, start : start + block_size] = True
    mask = causal_mask(np.arange(q.shape[0]), k.shape[0], window) & selected_mask
    k_exp, v_exp = expand_kv(k, v, q.shape[1])
    return attention(q, k_exp, v_exp, mask), selected
