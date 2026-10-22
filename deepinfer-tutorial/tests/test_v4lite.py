from __future__ import annotations

import numpy as np

from mini_deepinfer.config import ModelConfig
from mini_deepinfer.model.v4lite import CEDTransformer, MHCResidual, build_v4lite_model


def test_v4lite_moe_and_engram_forward() -> None:
    model = build_v4lite_model(seed=1, n_layers=2)
    logits, hidden, aux = model.forward_full([65, 66, 67, 68, 69])
    assert logits.shape == (5, model.config.vocab_size)
    assert hidden.shape == (5, model.config.d_model)
    assert 1 in aux["moe"]
    assert int(aux["moe"][1]["expert_counts"].sum()) == 5 * model.config.top_k_experts
    assert "layers.1.engram" in model.weights


def test_moe_counts_are_balanced_across_a_batch() -> None:
    model = build_v4lite_model(seed=2, n_layers=2)
    _, _, aux = model.forward_full(list(range(65, 97)))
    counts = aux["moe"][1]["expert_counts"]
    assert counts.sum() == 32 * model.config.top_k_experts
    assert np.count_nonzero(counts) >= 2


def test_ced_prefix_is_causal() -> None:
    config = ModelConfig(
        d_model=32,
        n_layers=2,
        n_heads=2,
        n_kv_heads=1,
        head_dim=16,
        ffn_dim=64,
        max_seq_len=32,
        window_size=8,
        n_encoder_layers=1,
        vocab_size=264,
    )
    model = CEDTransformer(config, seed=3)
    short = model.forward([65, 66, 67])
    longer = model.forward([65, 66, 67, 68, 69])
    np.testing.assert_allclose(short, longer[:3], atol=1e-5)


def test_mhc_residual_shape() -> None:
    rng = np.random.default_rng(0)
    x = rng.normal(size=(4, 32)).astype(np.float32)
    out = MHCResidual(32)(x)
    assert out.shape == x.shape
    assert np.isfinite(out).all()
