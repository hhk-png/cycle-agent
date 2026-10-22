from __future__ import annotations

import numpy as np

from mini_deepinfer.model.ops import attention, causal_mask
from mini_deepinfer.quantization import (
    kv_bytes_per_token,
    quantization_error,
    quantize_fp4,
    quantize_fp8,
)
from mini_deepinfer.sparse_attention import block_sparse_attention
from mini_deepinfer.vision import TinyViT, TinyViTConfig


def test_fp8_quantization_error_and_shape() -> None:
    rng = np.random.default_rng(0)
    x = rng.normal(size=(32, 64)).astype(np.float32)
    codes, scale, dequant = quantize_fp8(x)
    assert codes.shape == x.shape
    assert scale.shape == (1, 64)
    error = quantization_error(x, dequant)
    assert error["relative_l2"] < 0.05


def test_fp4_quantization_uses_blocks() -> None:
    rng = np.random.default_rng(1)
    x = rng.normal(size=(64,)).astype(np.float32)
    codes, scale, dequant = quantize_fp4(x, block_size=16)
    assert codes.size == x.size
    assert scale.size == 4
    assert dequant.shape == x.shape
    assert np.isfinite(dequant).all()
    assert quantization_error(x, dequant)["relative_l2"] < 0.35


def test_kv_bytes_accounts_for_bits_and_scales() -> None:
    fp16 = kv_bytes_per_token(40, 1, 128, 16)
    fp8 = kv_bytes_per_token(40, 1, 128, 8)
    fp4 = kv_bytes_per_token(40, 1, 128, 4, scale_bytes=1, scale_group=16)
    assert fp8 < fp16
    assert fp4 < fp8
    assert fp4 > 0


def test_block_sparse_attention_matches_dense_when_all_blocks_selected() -> None:
    rng = np.random.default_rng(2)
    q = rng.normal(size=(8, 4, 16)).astype(np.float32)
    k = rng.normal(size=(8, 2, 16)).astype(np.float32)
    v = rng.normal(size=(8, 2, 16)).astype(np.float32)
    dense = attention(
        q,
        np.repeat(k, 2, axis=1),
        np.repeat(v, 2, axis=1),
        causal_mask(np.arange(8), 8),
    )
    sparse, selected = block_sparse_attention(q, k, v, block_size=4, top_k=2)
    np.testing.assert_allclose(sparse, dense, atol=1e-6)
    assert selected.shape == (8, 2)


def test_block_sparse_attention_selects_a_subset() -> None:
    rng = np.random.default_rng(3)
    q = rng.normal(size=(8, 2, 8)).astype(np.float32)
    k = rng.normal(size=(8, 2, 8)).astype(np.float32)
    v = rng.normal(size=(8, 2, 8)).astype(np.float32)
    out, selected = block_sparse_attention(q, k, v, block_size=4, top_k=1)
    assert out.shape == (8, 2, 8)
    assert selected.shape == (8, 1)
    assert np.isfinite(out).all()


def test_tiny_vit_patch_count_and_determinism() -> None:
    config = TinyViTConfig(image_size=16, patch_size=4, out_dim=64, seed=4)
    encoder = TinyViT(config)
    image = np.random.default_rng(5).random((16, 16, 3)).astype(np.float32)
    tokens = encoder.encode(image)
    assert tokens.shape == (16, 64)
    np.testing.assert_allclose(tokens, encoder.encode(image), atol=1e-6)
    image2 = image + 0.1
    assert not np.allclose(tokens, encoder.encode(image2))
