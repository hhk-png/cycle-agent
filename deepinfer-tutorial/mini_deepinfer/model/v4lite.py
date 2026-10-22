"""Mechanism labs for V4.1-specific structures.

The shipped generation checkpoint is a dense model.  These classes add the
V4.1 structures that do not fit into a two-layer trained toy model:

* ``init_v4lite_weights`` gives a TinyTransformer MoE and Engram weights so
  that routing, shared experts and hashed memory can be exercised end to end;
* ``CEDTransformer`` implements the causal encoder-decoder idea, where the
  decoder's global K/V is projected from the encoder's final hidden state;
* ``MHCResidual`` is a two-stream residual mixer inspired by mHC.

They are intentionally small mechanism labs, not trained checkpoints.  The
tutorial calls this out explicitly.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from ..config import ModelConfig
from .ops import apply_rope, attention, causal_mask, expand_kv, rms_norm, rope_tables, silu, softmax
from .tiny_transformer import TinyTransformer, engram_hash
from .training import init_weights


def init_v4lite_weights(config: ModelConfig, seed: int = 0) -> dict[str, np.ndarray]:
    """Dense weights plus MoE/Engram tables for mechanism tests."""
    rng = np.random.default_rng(seed)
    weights = init_weights(config, seed)
    d = config.d_model
    if config.use_moe:
        for layer in config.moe_layers:
            prefix = f"layers.{layer}.moe"
            weights[f"{prefix}.gate"] = rng.normal(0.0, 0.02, (d, config.n_experts)).astype(np.float32)
            for name in ("w1", "w3"):
                weights[f"{prefix}.shared.{name}"] = rng.normal(0.0, 0.02, (d, config.ffn_dim)).astype(np.float32)
            weights[f"{prefix}.shared.w2"] = rng.normal(0.0, 0.02, (config.ffn_dim, d)).astype(np.float32)
            for expert in range(config.n_experts):
                for name in ("w1", "w3"):
                    weights[f"{prefix}.experts.{expert}.{name}"] = rng.normal(
                        0.0, 0.02, (d, config.ffn_dim)
                    ).astype(np.float32)
                weights[f"{prefix}.experts.{expert}.w2"] = rng.normal(
                    0.0, 0.02, (config.ffn_dim, d)
                ).astype(np.float32)
    if config.use_engram:
        weights[f"layers.{config.engram_layer}.engram"] = rng.normal(
            0.0, 0.02, (config.engram_buckets, d)
        ).astype(np.float32)
    return weights


def build_v4lite_model(
    seed: int = 0,
    *,
    n_layers: int = 2,
    d_model: int = 64,
    n_heads: int = 4,
    n_kv_heads: int = 2,
    head_dim: int = 16,
    ffn_dim: int = 128,
    n_experts: int = 8,
    top_k: int = 2,
) -> TinyTransformer:
    config = ModelConfig(
        d_model=d_model,
        n_layers=n_layers,
        n_heads=n_heads,
        n_kv_heads=n_kv_heads,
        head_dim=head_dim,
        ffn_dim=ffn_dim,
        max_seq_len=256,
        window_size=64,
        swa_layers=(0,),
        use_moe=True,
        n_experts=n_experts,
        top_k_experts=top_k,
        moe_layers=(1,),
        use_engram=True,
        engram_layer=1,
    )
    return TinyTransformer(config, init_v4lite_weights(config, seed))


@dataclass
class CEDWeights:
    tok_emb: np.ndarray
    lnf: np.ndarray
    enc: list[dict[str, np.ndarray]]
    dec: list[dict[str, np.ndarray]]
    ced_k: np.ndarray
    ced_v: np.ndarray


class CEDTransformer:
    """A causal encoder-decoder with a shared global KV projection.

    The encoder layers process the prompt.  The decoder layers do not build a
    per-layer global KV; they project the encoder's final hidden once into
    global K/V and combine it with a local sliding-window attention path.
    """

    def __init__(self, config: ModelConfig, weights: CEDWeights | None = None, seed: int = 0) -> None:
        if config.n_encoder_layers < 1 or config.n_encoder_layers >= config.n_layers:
            raise ValueError("CED requires at least one encoder and one decoder layer")
        self.config = config
        self.cos, self.sin = rope_tables(config.max_seq_len, config.head_dim, config.rope_theta)
        self.weights = weights or self.init_weights(seed)

    def init_weights(self, seed: int = 0) -> CEDWeights:
        rng = np.random.default_rng(seed)
        cfg = self.config
        d, kv = cfg.d_model, cfg.kv_dim
        def block() -> dict[str, np.ndarray]:
            return {
                "ln1": np.ones(d, dtype=np.float32),
                "ln2": np.ones(d, dtype=np.float32),
                "wq": rng.normal(0, 0.02, (d, d)).astype(np.float32),
                "wk": rng.normal(0, 0.02, (d, kv)).astype(np.float32),
                "wv": rng.normal(0, 0.02, (d, kv)).astype(np.float32),
                "wo": rng.normal(0, 0.02, (d, d)).astype(np.float32),
                "w1": rng.normal(0, 0.02, (d, cfg.ffn_dim)).astype(np.float32),
                "w3": rng.normal(0, 0.02, (d, cfg.ffn_dim)).astype(np.float32),
                "w2": rng.normal(0, 0.02, (cfg.ffn_dim, d)).astype(np.float32),
            }
        return CEDWeights(
            tok_emb=rng.normal(0, 0.02, (cfg.vocab_size, d)).astype(np.float32),
            lnf=np.ones(d, dtype=np.float32),
            enc=[block() for _ in range(cfg.n_encoder_layers)],
            dec=[block() for _ in range(cfg.n_decoder_layers)],
            ced_k=rng.normal(0, 0.02, (d, kv)).astype(np.float32),
            ced_v=rng.normal(0, 0.02, (d, kv)).astype(np.float32),
        )

    def _attention_block(
        self,
        x: np.ndarray,
        weights: dict[str, np.ndarray],
        positions: np.ndarray,
        key_values: tuple[np.ndarray, np.ndarray] | None,
        window: int | None,
    ) -> np.ndarray:
        cfg = self.config
        h, _ = rms_norm(x, weights["ln1"], cfg.rms_norm_eps)
        q = (h @ weights["wq"]).reshape(-1, cfg.n_heads, cfg.head_dim)
        k = (h @ weights["wk"]).reshape(-1, cfg.n_kv_heads, cfg.head_dim)
        v = (h @ weights["wv"]).reshape(-1, cfg.n_kv_heads, cfg.head_dim)
        q = apply_rope(q, positions, self.cos, self.sin)
        k = apply_rope(k, positions, self.cos, self.sin)
        if key_values is not None:
            global_k, global_v = key_values
            all_k = np.concatenate([k, global_k], axis=0)
            all_v = np.concatenate([v, global_v], axis=0)
            local_mask = causal_mask(positions, k.shape[0], window)
            global_mask = np.arange(global_k.shape[0])[None, :] <= positions[:, None]
            mask = np.concatenate([local_mask, global_mask], axis=1)
        else:
            all_k, all_v = k, v
            mask = causal_mask(positions, k.shape[0], window)
        all_k, all_v = expand_kv(all_k, all_v, cfg.n_heads)
        context = attention(q, all_k, all_v, mask).reshape(-1, cfg.n_heads * cfg.head_dim)
        out = context @ weights["wo"]
        x = x + out
        h, _ = rms_norm(x, weights["ln2"], cfg.rms_norm_eps)
        x = x + (silu(h @ weights["w1"]) * (h @ weights["w3"])) @ weights["w2"]
        return x

    def forward(self, token_ids: list[int]) -> np.ndarray:
        cfg = self.config
        positions = np.arange(len(token_ids), dtype=np.int64)
        x = self.weights.tok_emb[np.asarray(token_ids, dtype=np.int64)]
        for layer in self.weights.enc:
            x = self._attention_block(x, layer, positions, None, None)
        encoder_hidden = x
        global_k = (encoder_hidden @ self.weights.ced_k).reshape(-1, cfg.n_kv_heads, cfg.head_dim)
        global_v = (encoder_hidden @ self.weights.ced_v).reshape(-1, cfg.n_kv_heads, cfg.head_dim)
        for layer in self.weights.dec:
            x = self._attention_block(
                x, layer, positions, (global_k, global_v), cfg.window_size
            )
        hidden, _ = rms_norm(x, self.weights.lnf, cfg.rms_norm_eps)
        return hidden @ self.weights.tok_emb.T


class MHCResidual:
    """Two-stream residual mixing, a tiny stand-in for mHC.

    Each token carries two residual views; a learned 2x2 mix combines them.
    The real mHC fuses several residual streams into the attention/MLP kernels.
    """

    def __init__(self, d_model: int, seed: int = 0) -> None:
        rng = np.random.default_rng(seed)
        self.mix = rng.normal(0, 0.1, (2, 2)).astype(np.float32)
        self.out = rng.normal(0, 0.02, (2 * d_model, d_model)).astype(np.float32)

    def __call__(self, x: np.ndarray) -> np.ndarray:
        streams = np.stack([x, np.tanh(x)], axis=1)  # [T, 2, D]
        mixed = np.einsum("ij,tjd->tid", self.mix, streams)
        return mixed.reshape(x.shape[0], -1) @ self.out
