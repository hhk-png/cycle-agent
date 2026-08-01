"""A tiny GPT-style transformer written in pure NumPy.

This is the analogue of ``vllm/model_executor/models/gpt2.py`` (and its
Triton attention backend).  All heavy lifting is done with ``np.einsum`` so
the code mirrors the algebra, not the hardware.

Two important design points, both mirroring vLLM:

1. The **weights are parameters of the model object**; the engine reads them
   with ``state_dict()`` / writes them with ``load_state_dict()`` (the NumPy
   equivalent of HuggingFace ``torch.load`` / ``save_pretrained``).
2. **KV cache is stored externally** (in the ``KVStore``).  ``forward()``
   computes new K/V, writes them into the caller-provided physical blocks and
   then runs paged attention.  The model never sees a full contiguous KV
   matrix, which is exactly the PagedAttention contract.
"""

from __future__ import annotations

from typing import Dict, List, Optional

import numpy as np

from .attention import paged_attention_batch
from .config import ModelConfig


def _layernorm(x: np.ndarray, w: np.ndarray, b: np.ndarray, eps: float = 1e-5) -> np.ndarray:
    mean = x.mean(axis=-1, keepdims=True)
    var = x.var(axis=-1, keepdims=True)
    return (x - mean) / np.sqrt(var + eps) * w + b


def _gelu(x: np.ndarray) -> np.ndarray:
    # GPT-2's GELU (tanh approximation)
    return 0.5 * x * (1.0 + np.tanh(np.sqrt(2.0 / np.pi) * (x + 0.044715 * x ** 3)))


class TinyGPT:
    def __init__(self, config: ModelConfig, seed: int = 0,
                 weights: Optional[Dict[str, np.ndarray]] = None):
        self.config = config
        d, v, n = config.n_embd, config.vocab_size, config.n_layer
        scale = 0.02  # GPT-2 init std
        rng = np.random.default_rng(seed)

        def rnd(*shape):
            return rng.normal(0.0, scale, size=shape).astype(np.float32)

        # word + position embeddings
        self.wte = rnd(v, d)
        self.wpe = rnd(config.block_size, d)

        # transformer blocks
        self.ln1_w, self.ln1_b = [], []
        self.c_attn_w, self.c_attn_b = [], []
        self.c_proj_w, self.c_proj_b = [], []
        self.ln2_w, self.ln2_b = [], []
        self.c_mlp_w, self.c_mlp_b = [], []
        self.c_mlp_p_w, self.c_mlp_p_b = [], []
        for _ in range(n):
            self.ln1_w.append(np.ones(d, np.float32))
            self.ln1_b.append(np.zeros(d, np.float32))
            self.c_attn_w.append(rnd(d, 3 * d))
            self.c_attn_b.append(np.zeros(3 * d, np.float32))
            self.c_proj_w.append(rnd(d, d))
            self.c_proj_b.append(np.zeros(d, np.float32))
            self.ln2_w.append(np.ones(d, np.float32))
            self.ln2_b.append(np.zeros(d, np.float32))
            self.c_mlp_w.append(rnd(d, 4 * d))
            self.c_mlp_b.append(np.zeros(4 * d, np.float32))
            self.c_mlp_p_w.append(rnd(4 * d, d))
            self.c_mlp_p_b.append(np.zeros(d, np.float32))

        # final layer norm; lm_head is the tied embedding matrix
        self.ln_f_w = np.ones(d, np.float32)
        self.ln_f_b = np.zeros(d, np.float32)

        if weights is not None:
            self.load_state_dict(weights)

    # ------------------------------------------------------------------ #
    # checkpoint helpers
    # ------------------------------------------------------------------ #
    def state_dict(self) -> Dict[str, np.ndarray]:
        return {k: v for k, v in self.__dict__.items() if k != "config"}

    def load_state_dict(self, sd: Dict[str, np.ndarray]) -> None:
        for k, v in sd.items():
            if not hasattr(self, k):
                raise KeyError(f"unknown parameter '{k}'")
            setattr(self, k, v)

    # ------------------------------------------------------------------ #
    # inference forward (paged attention)
    # ------------------------------------------------------------------ #
    def forward(self, x: np.ndarray, positions: np.ndarray,
                seqs: List, block_manager) -> np.ndarray:
        """Run the transformer for a batch of new tokens.

        Args:
            x:         ``(B, T)`` token ids of the tokens being processed now.
            positions: ``(B, T)`` absolute token positions in each sequence.
            seqs:      list of ``Sequence`` objects (for their block tables).
            block_manager: owns the KV store; its ``write_kv`` handles COW.

        Returns:
            logits ``(B, T, vocab_size)`` for the new tokens.  Only the last
            column is needed for sampling during decode.
        """
        cfg = self.config
        B, T = x.shape
        d = cfg.n_embd

        tok_emb = self.wte[x]                       # (B, T, d)
        pos_emb = self.wpe[positions]               # (B, T, d)
        h = tok_emb + pos_emb

        kv_store = block_manager.kv_store
        block_tables = [block_manager.get_block_table(s) for s in seqs]
        cached_lens = np.array([s.cached_len for s in seqs], dtype=np.int64)

        for layer in range(cfg.n_layer):
            # --- attention -------------------------------------------------
            ln1 = _layernorm(h, self.ln1_w[layer], self.ln1_b[layer])
            qkv = ln1 @ self.c_attn_w[layer] + self.c_attn_b[layer]
            q, k, v = np.split(qkv, 3, axis=-1)

            q = q.reshape(B, T, cfg.n_head, cfg.head_dim).transpose(0, 2, 1, 3)
            k = k.reshape(B, T, cfg.n_head, cfg.head_dim).transpose(0, 2, 1, 3)
            v = v.reshape(B, T, cfg.n_head, cfg.head_dim).transpose(0, 2, 1, 3)

            # store the newly computed K/V into the paged cache
            for b in range(B):
                for t in range(T):
                    logical_pos = positions[b, t]
                    block_manager.write_kv(layer, seqs[b], int(logical_pos),
                                           k[b, :, t, :], v[b, :, t, :])

            attn = paged_attention_batch(q, k, v, cached_lens, block_tables,
                                         kv_store, layer, cfg.n_head,
                                         cfg.head_dim, block_manager.block_size)
            attn = attn.transpose(0, 2, 1, 3).reshape(B, T, d)  # (B, T, d)
            h = h + attn @ self.c_proj_w[layer] + self.c_proj_b[layer]

            # --- MLP --------------------------------------------------------
            ln2 = _layernorm(h, self.ln2_w[layer], self.ln2_b[layer])
            mlp = ln2 @ self.c_mlp_w[layer] + self.c_mlp_b[layer]
            mlp = _gelu(mlp)
            mlp = mlp @ self.c_mlp_p_w[layer] + self.c_mlp_p_b[layer]
            h = h + mlp

        h = _layernorm(h, self.ln_f_w, self.ln_f_b)
        logits = h @ self.wte.T                       # tied lm_head
        return logits
