"""A compact decoder-only Transformer used to drive the engine.

It is deliberately small and readable.  The important part for the tutorial is
not language quality but the fact that every engine mechanism (paged KV,
chunked prefill, speculation, SWA, MoE routing, structured masks) can be
exercised with real matrix maths.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np

from ..cache_engine import PagedKVCache
from ..config import ModelConfig
from ..sequence import Sequence
from .ops import (
    apply_rope,
    attention,
    causal_mask,
    expand_kv,
    rms_norm,
    rope_tables,
    silu,
    softmax,
)


def engram_hash(token_ids: list[int], position: int, ngram: int, buckets: int) -> int:
    """Stable hash of the n-gram ending at ``position``.

    A real Engram table is sharded across ranks and fetched by hash.  The
    polynomial hash below has the same lookup semantics at toy scale.
    """
    value = 0
    for offset in range(ngram - 1, -1, -1):
        index = position - offset
        token = token_ids[index] if index >= 0 else 0
        value = (value * 1_000_003 + token + 1) % buckets
    return value


class TinyTransformer:
    def __init__(self, config: ModelConfig, weights: dict[str, np.ndarray]) -> None:
        self.config = config
        self.weights = weights
        self.cos, self.sin = rope_tables(
            config.max_seq_len, config.head_dim, config.rope_theta
        )
        self._validate_weights()

    # ------------------------------------------------------------------
    # Loading / metadata
    # ------------------------------------------------------------------
    @classmethod
    def from_file(cls, path: str | Path) -> TinyTransformer:
        data = np.load(Path(path), allow_pickle=False)
        config_raw = data["config_json"]
        config_dict = json.loads(str(config_raw.item() if config_raw.ndim == 0 else config_raw[0]))
        config = ModelConfig(**config_dict)
        weights = {key: data[key] for key in data.files if key != "config_json"}
        return cls(config, weights)

    def save(self, path: str | Path) -> None:
        payload = {"config_json": np.array(json.dumps(self.config.__dict__, default=list))}
        payload.update(self.weights)
        np.savez_compressed(Path(path), **payload)

    def _validate_weights(self) -> None:
        required = ["tok_emb", "lnf"]
        missing = [key for key in required if key not in self.weights]
        if missing:
            raise ValueError(f"missing weights: {missing}")
        if self.weights["tok_emb"].shape[0] != self.config.vocab_size:
            raise ValueError("token embedding does not match vocab_size")

    @property
    def num_parameters(self) -> int:
        return int(sum(array.size for array in self.weights.values()))

    def _has_moe(self, layer: int) -> bool:
        return self.config.use_moe and layer in self.config.moe_layers

    def _layer_weight(self, layer: int, name: str) -> np.ndarray:
        return self.weights[f"layers.{layer}.{name}"]

    # ------------------------------------------------------------------
    # Layer primitives
    # ------------------------------------------------------------------
    def _ffn(self, layer: int, h: np.ndarray) -> np.ndarray:
        w1 = self._layer_weight(layer, "w1")
        w3 = self._layer_weight(layer, "w3")
        w2 = self._layer_weight(layer, "w2")
        return (silu(h @ w1) * (h @ w3)) @ w2

    def _expert_ffn(self, layer: int, expert: int, h: np.ndarray) -> np.ndarray:
        prefix = f"layers.{layer}.moe.experts.{expert}"
        w1 = self.weights[f"{prefix}.w1"]
        w3 = self.weights[f"{prefix}.w3"]
        w2 = self.weights[f"{prefix}.w2"]
        return (silu(h @ w1) * (h @ w3)) @ w2

    def _shared_ffn(self, layer: int, h: np.ndarray) -> np.ndarray:
        prefix = f"layers.{layer}.moe.shared"
        w1 = self.weights[f"{prefix}.w1"]
        w3 = self.weights[f"{prefix}.w3"]
        w2 = self.weights[f"{prefix}.w2"]
        return (silu(h @ w1) * (h @ w3)) @ w2

    def _moe(
        self, layer: int, h: np.ndarray
    ) -> tuple[np.ndarray, dict[str, np.ndarray]]:
        gate = h @ self.weights[f"layers.{layer}.moe.gate"]
        probs = softmax(gate, axis=-1)
        top_k = self.config.top_k_experts
        top_indices = np.argpartition(-probs, kth=top_k - 1, axis=-1)[:, :top_k]
        top_weights = np.take_along_axis(probs, top_indices, axis=-1)
        top_weights = top_weights / np.sum(top_weights, axis=-1, keepdims=True)

        out = self._shared_ffn(layer, h)
        counts = np.zeros(self.config.n_experts, dtype=np.int64)
        for slot in range(top_k):
            for expert in range(self.config.n_experts):
                mask = top_indices[:, slot] == expert
                if not np.any(mask):
                    continue
                expert_out = self._expert_ffn(layer, expert, h[mask])
                out[mask] += top_weights[mask, slot, None] * expert_out
                counts[expert] += int(np.sum(mask))
        return out, {"expert_counts": counts, "router_probs": probs}

    def _engram(self, layer: int, x: np.ndarray, token_ids: list[int], positions: np.ndarray) -> np.ndarray:
        table = self.weights.get(f"layers.{layer}.engram")
        if table is None:
            return x
        ids = [
            engram_hash(token_ids, int(pos), self.config.engram_ngram, self.config.engram_buckets)
            for pos in positions
        ]
        return x + table[np.asarray(ids, dtype=np.int64)]

    def _attention(
        self,
        layer: int,
        x: np.ndarray,
        positions: np.ndarray,
        *,
        seq: Sequence | None,
        start: int,
        end: int,
        kv_cache: PagedKVCache | None,
        token_ids: list[int],
    ) -> np.ndarray:
        cfg = self.config
        h, _ = rms_norm(x, self._layer_weight(layer, "ln1"), cfg.rms_norm_eps)
        q = (h @ self._layer_weight(layer, "wq")).reshape(-1, cfg.n_heads, cfg.head_dim)
        k = (h @ self._layer_weight(layer, "wk")).reshape(-1, cfg.n_kv_heads, cfg.head_dim)
        v = (h @ self._layer_weight(layer, "wv")).reshape(-1, cfg.n_kv_heads, cfg.head_dim)
        q = apply_rope(q, positions, self.cos, self.sin)
        k = apply_rope(k, positions, self.cos, self.sin)

        if kv_cache is not None and seq is not None:
            kv_cache.write_layer(layer, seq, start, k, v)
            all_k, all_v = kv_cache.gather_layer(layer, seq, end)
        else:
            all_k, all_v = k, v

        all_k, all_v = expand_kv(all_k, all_v, cfg.n_heads)
        window = cfg.window_size if layer in cfg.swa_layers else None
        mask = causal_mask(positions, all_k.shape[0], window)
        context = attention(q, all_k, all_v, mask)
        context = context.reshape(-1, cfg.n_heads * cfg.head_dim)
        return context @ self._layer_weight(layer, "wo")

    # ------------------------------------------------------------------
    # Forward passes
    # ------------------------------------------------------------------
    def _run(
        self,
        token_ids: list[int],
        *,
        seq: Sequence | None,
        start: int,
        end: int,
        kv_cache: PagedKVCache | None,
    ) -> tuple[np.ndarray, np.ndarray, dict[str, Any]]:
        cfg = self.config
        positions = np.arange(start, end, dtype=np.int64)
        x = self.weights["tok_emb"][np.asarray(token_ids[start:end], dtype=np.int64)]
        aux: dict[str, Any] = {"moe": {}}
        for layer in range(cfg.n_layers):
            if cfg.use_engram and layer == cfg.engram_layer:
                x = self._engram(layer, x, token_ids, positions)
            x = x + self._attention(
                layer,
                x,
                positions,
                seq=seq,
                start=start,
                end=end,
                kv_cache=kv_cache,
                token_ids=token_ids,
            )
            h, _ = rms_norm(x, self._layer_weight(layer, "ln2"), cfg.rms_norm_eps)
            if self._has_moe(layer):
                ffn_out, moe_aux = self._moe(layer, h)
                aux["moe"][layer] = moe_aux
            else:
                ffn_out = self._ffn(layer, h)
            x = x + ffn_out
        hidden, _ = rms_norm(x, self.weights["lnf"], cfg.rms_norm_eps)
        if cfg.tie_embeddings:
            logits = hidden @ self.weights["tok_emb"].T
        else:
            logits = hidden @ self.weights["lm_head"].T
        return logits, hidden, aux

    def forward_full(self, token_ids: list[int]) -> tuple[np.ndarray, np.ndarray, dict[str, Any]]:
        """Teacher-forced forward over a complete sequence (used by training)."""
        return self._run(token_ids, seq=None, start=0, end=len(token_ids), kv_cache=None)

    def forward_paged(
        self,
        seq: Sequence,
        start: int,
        end: int,
        kv_cache: PagedKVCache,
        token_ids: list[int] | None = None,
    ) -> tuple[np.ndarray, np.ndarray, dict[str, Any]]:
        """Compute tokens ``[start, end)`` and update the paged KV cache.

        ``token_ids`` lets speculative verification feed draft tokens that are
        not yet part of the sequence's committed output.
        """
        ids = seq.all_token_ids() if token_ids is None else token_ids
        if start < 0 or end > len(ids) or start >= end:
            raise ValueError("invalid paged forward range")
        return self._run(
            ids,
            seq=seq,
            start=start,
            end=end,
            kv_cache=kv_cache,
        )


def load_model(path: str | Path) -> TinyTransformer:
    return TinyTransformer.from_file(path)
