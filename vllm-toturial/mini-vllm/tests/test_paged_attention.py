"""Verify PagedAttention against a dense reference implementation.

The point: the paged path must produce *exactly* the same numbers as plain,
dense causal attention over a contiguous KV matrix, even though the KV cache
is scattered across non-contiguous physical blocks.
"""

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from minivllm.attention import paged_attention_batch  # noqa: E402
from minivllm.config import CacheConfig, ModelConfig  # noqa: E402
from minivllm.kv_cache import KVStore  # noqa: E402


def _softmax_rows(x):
    e = np.exp(x - np.max(x, axis=-1, keepdims=True))
    return e / e.sum(axis=-1, keepdims=True)


def _dense_reference(q, k, v, k_cached, v_cached, cached_lens, head_dim):
    """Dense causal attention with full (contiguous) cached K/V."""
    B, nh, T, hd = q.shape
    outs = []
    for b in range(B):
        clen = int(cached_lens[b])
        new_k = k[b].transpose(1, 0, 2)
        new_v = v[b].transpose(1, 0, 2)
        k_full = np.concatenate([k_cached[b], new_k], axis=0) if clen else new_k
        v_full = np.concatenate([v_cached[b], new_v], axis=0) if clen else new_v
        scores = np.einsum("hit,jht->hij", q[b], k_full) / np.sqrt(head_dim)
        key_pos = np.arange(clen + T)[None, :]
        query_pos = clen + np.arange(T)[:, None]
        scores = np.where(key_pos > query_pos, -np.inf, scores)
        probs = _softmax_rows(scores)
        out = np.einsum("hij,jht->hit", probs, v_full)
        outs.append(out)
    return np.stack(outs, axis=0)


def test_paged_matches_dense():
    rng = np.random.default_rng(0)
    cfg = CacheConfig(block_size=4, num_gpu_blocks=64)
    mc = ModelConfig(vocab_size=20, n_embd=8, n_layer=2, n_head=2, head_dim=4,
                     block_size=16)
    kv = KVStore(cfg, mc)

    B, nh, T, hd = 3, 2, 2, 4
    q = rng.normal(size=(B, nh, T, hd)).astype(np.float32)
    k = rng.normal(size=(B, nh, T, hd)).astype(np.float32)
    v = rng.normal(size=(B, nh, T, hd)).astype(np.float32)
    cached_lens = np.array([3, 0, 5], dtype=np.int64)

    # Build contiguous cached K/V, then scatter each into distinct physical blocks
    k_cached_full, v_cached_full, block_tables = [], [], []
    total_blocks = sum((cl + cfg.block_size - 1) // cfg.block_size for cl in cached_lens)
    physical = rng.choice(cfg.num_gpu_blocks, size=total_blocks, replace=False).tolist()
    ptr = 0
    for b in range(B):
        clen = int(cached_lens[b])
        kc = rng.normal(size=(clen, nh, hd)).astype(np.float32)
        vc = rng.normal(size=(clen, nh, hd)).astype(np.float32)
        k_cached_full.append(kc)
        v_cached_full.append(vc)
        nblocks = (clen + cfg.block_size - 1) // cfg.block_size
        table = physical[ptr:ptr + nblocks]
        ptr += nblocks
        block_tables.append(table)
        for bi in range(nblocks):
            phys = table[bi]
            # logical positions [bi*bs, min((bi+1)*bs, clen)) go to block
            # offsets [0, length)
            start = bi * cfg.block_size
            end = min((bi + 1) * cfg.block_size, clen)
            length = end - start
            kv.k_cache[0][phys, :length] = kc[start:end]
            kv.v_cache[0][phys, :length] = vc[start:end]

    paged = paged_attention_batch(q, k, v, cached_lens, block_tables, kv, 0,
                                  mc.n_head, mc.head_dim, cfg.block_size)
    dense = _dense_reference(q, k, v, k_cached_full, v_cached_full,
                             cached_lens, mc.head_dim)
    np.testing.assert_allclose(paged, dense, rtol=1e-5, atol=1e-6)


def test_no_cache_matches_dense():
    """Single sequence, nothing cached (pure prefill attention)."""
    rng = np.random.default_rng(1)
    cfg = CacheConfig(block_size=8, num_gpu_blocks=16)
    mc = ModelConfig(n_embd=8, n_layer=1, n_head=2, head_dim=4, block_size=16)
    kv = KVStore(cfg, mc)
    B, nh, T, hd = 1, 2, 4, 4
    q = rng.normal(size=(B, nh, T, hd)).astype(np.float32)
    k = rng.normal(size=(B, nh, T, hd)).astype(np.float32)
    v = rng.normal(size=(B, nh, T, hd)).astype(np.float32)
    cached_lens = np.zeros(B, dtype=np.int64)
    block_tables = [[] for _ in range(B)]

    paged = paged_attention_batch(q, k, v, cached_lens, block_tables, kv, 0,
                                  mc.n_head, mc.head_dim, cfg.block_size)
    dense = _dense_reference(q, k, v, [], [], cached_lens, mc.head_dim)
    np.testing.assert_allclose(paged, dense, rtol=1e-5, atol=1e-6)


if __name__ == "__main__":
    test_no_cache_matches_dense()
    test_paged_matches_dense()
    print("test_paged_attention: OK")
