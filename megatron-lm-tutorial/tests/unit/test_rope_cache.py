"""Regression tests for the RoPE cache.

The cache is built lazily on the device of the first forward.  A CPU prefill
followed by a CUDA decode is a common real-world sequence (e.g. weight loading
or checkpoint inspection on CPU).  The cache must be rebuilt without assuming
that the requested length equals the already-cached length.
"""
from __future__ import annotations

import torch

from mini_megatron.parallel.attention import RotaryEmbedding


def test_rope_cache_grows_and_shrinks_on_device_change():
    rope = RotaryEmbedding(head_dim=8, max_seq_len=16)
    assert rope._cos is not None and rope._cos.shape == (16, 8)

    # Rebuild on a different device with a *shorter* request.  Before the fix
    # this raised a reshape error because arange used the old cache length but
    # reshape used the requested length.
    rope._build_cache(4, torch.device("meta"))
    assert rope._cos is not None and rope._cos.shape == (16, 8)
    assert rope._cos.device.type == "meta"

    # A longer request rebuilds on the current device.
    rope._build_cache(20, torch.device("meta"))
    assert rope._cos is not None and rope._cos.shape == (20, 8)


def test_interleaved_cache_shape_matches_head_dim():
    rope = RotaryEmbedding(head_dim=8, max_seq_len=4, interleaved=True)
    q = torch.randn(1, 2, 4, 8)
    k = torch.randn(1, 2, 4, 8)
    pos = torch.arange(4).unsqueeze(0)
    q2, k2 = rope(q, k, pos)
    assert q2.shape == q.shape and k2.shape == k.shape
    # Interleaved layout pairs adjacent channels: cos[0] == cos[1].
    assert torch.allclose(rope._cos[:, 0], rope._cos[:, 1])
