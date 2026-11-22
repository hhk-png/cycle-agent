"""Multi-head Latent Attention (MLA): a runnable, standalone reference.

This module is an **extension**, not part of :class:`TransformerStage`.  It
exists because MLA (DeepSeek-V2/V3, and the ``multi_latent_attention`` path in
Megatron Core) is the clearest example of a model change that is also a
*system* change: the KV cache shrinks from ``2 * n_heads * head_dim`` per token
to ``kv_lora_rank + qk_rope_head_dim``, but the projection graph and the
inference-time weight absorption become more complex.

The implementation deliberately uses plain ``nn.Linear`` (no TP/PP/CP) so the
tensor shapes and the cache semantics are readable.  Chapter 30 maps each
matrix to its tensor-parallel sharding; the numerical contract is the same.

Shape notation (per batch, sequence length ``s``):

    x            [b, s, h]
    q            [b, s, n * (d_nope + d_rope)]
    c_kv         [b, s, d_c]                 # the only large cached tensor
    k_pe         [b, s, d_rope]              # shared by all heads
    k_nope, v    [b, s, n, d_nope / d_v]

The attention score is the sum of a content term and a decoupled RoPE term:

    score = q_nope @ k_nope^T + q_pe @ k_pe^T

Only ``q_pe``/``k_pe`` receive RoPE.  Applying RoPE to the full K (including
``k_nope``) is a common and silent conversion bug.
"""
from __future__ import annotations

import math
from typing import Optional, Tuple

import torch
import torch.nn as nn
import torch.nn.functional as F

from .attention import RotaryEmbedding


class MLAttention(nn.Module):
    """DeepSeek-style multi-head latent attention.

    Parameters
    ----------
    hidden_size:
        Model width ``h``.
    num_heads:
        Number of query heads ``n``.
    q_lora_rank:
        If set, queries go through a low-rank bottleneck
        ``q_a_proj -> q_lora_rank -> q_b_proj``.  ``None`` uses a single
        ``q_proj`` (the DeepSeek-V2-Lite choice).
    kv_lora_rank:
        Latent KV width ``d_c``.  This is what the cache stores.
    qk_nope_head_dim / qk_rope_head_dim / v_head_dim:
        Per-head content, RoPE and value dimensions.  DeepSeek-V2 uses
        ``128 / 64 / 128``; the defaults here are small enough for unit tests.
    """

    def __init__(
        self,
        hidden_size: int,
        num_heads: int,
        *,
        q_lora_rank: Optional[int] = None,
        kv_lora_rank: int = 64,
        qk_nope_head_dim: int = 32,
        qk_rope_head_dim: int = 16,
        v_head_dim: int = 32,
        max_seq_len: int = 128,
        rope_base: float = 10000.0,
        attention_dropout: float = 0.0,
        bias: bool = False,
    ) -> None:
        super().__init__()
        if hidden_size <= 0 or num_heads <= 0:
            raise ValueError("hidden_size and num_heads must be positive")
        if kv_lora_rank <= 0:
            raise ValueError("kv_lora_rank must be positive")
        if qk_rope_head_dim <= 0 or qk_rope_head_dim % 2 != 0:
            raise ValueError("qk_rope_head_dim must be a positive even number")
        if qk_nope_head_dim <= 0 or v_head_dim <= 0:
            raise ValueError("qk_nope_head_dim and v_head_dim must be positive")

        self.hidden_size = hidden_size
        self.num_heads = num_heads
        self.q_lora_rank = q_lora_rank
        self.kv_lora_rank = kv_lora_rank
        self.qk_nope_head_dim = qk_nope_head_dim
        self.qk_rope_head_dim = qk_rope_head_dim
        self.v_head_dim = v_head_dim
        self.qk_head_dim = qk_nope_head_dim + qk_rope_head_dim
        self.scale = 1.0 / math.sqrt(self.qk_head_dim)
        self.attention_dropout = attention_dropout

        if q_lora_rank is not None:
            self.q_a_proj = nn.Linear(hidden_size, q_lora_rank, bias=bias)
            self.q_b_proj = nn.Linear(
                q_lora_rank, num_heads * self.qk_head_dim, bias=bias
            )
            self.q_proj = None
        else:
            self.q_a_proj = None
            self.q_b_proj = None
            self.q_proj = nn.Linear(
                hidden_size, num_heads * self.qk_head_dim, bias=bias
            )

        # One joint down-projection produces the cached latent and the shared
        # RoPE key.  The name follows the HF checkpoint:
        # ``kv_a_proj_with_mqa``.  "MQA" here means the RoPE key has one head,
        # not that values are shared.
        self.kv_a_proj_with_mqa = nn.Linear(
            hidden_size, kv_lora_rank + qk_rope_head_dim, bias=bias
        )
        # Up-projection: latent -> per-head K_nope and V.
        self.kv_b_proj = nn.Linear(
            kv_lora_rank, num_heads * (qk_nope_head_dim + v_head_dim), bias=bias
        )
        self.o_proj = nn.Linear(num_heads * v_head_dim, hidden_size, bias=bias)
        self.rotary_emb = RotaryEmbedding(
            qk_rope_head_dim, max_seq_len, base=rope_base
        )

    # ------------------------------------------------------------------
    # Projection helpers
    # ------------------------------------------------------------------
    def _project_q(self, x: torch.Tensor) -> torch.Tensor:
        if self.q_proj is not None:
            return self.q_proj(x)
        assert self.q_a_proj is not None and self.q_b_proj is not None
        return self.q_b_proj(self.q_a_proj(x))

    def _split_kv(
        self, c_kv: torch.Tensor
    ) -> Tuple[torch.Tensor, torch.Tensor]:
        """Map the latent to per-head K_nope and V.

        Keeping this as a method makes the inference-time absorption trick
        explicit: ``kv_b_proj`` can be folded into the query/output projections
        when the decoder is deployed with a fixed cache layout (chapter 30).
        """
        b, s, _ = c_kv.shape
        kv = self.kv_b_proj(c_kv).view(
            b, s, self.num_heads, self.qk_nope_head_dim + self.v_head_dim
        )
        k_nope = kv[..., : self.qk_nope_head_dim]
        v = kv[..., self.qk_nope_head_dim :]
        return k_nope, v

    # ------------------------------------------------------------------
    # Forward
    # ------------------------------------------------------------------
    def forward(
        self,
        x: torch.Tensor,
        *,
        position_ids: torch.Tensor,
        attention_mask: Optional[torch.Tensor] = None,
        past_key_value: Optional[Tuple[torch.Tensor, torch.Tensor]] = None,
        use_cache: bool = False,
    ):
        """Run MLA.

        ``past_key_value`` is ``(c_kv, k_pe)`` -- the *compressed* cache.  It is
        not a full K/V cache.  Passing it makes this call an incremental decode
        step; ``position_ids`` then contains only the new token positions.

        Returns ``out`` or ``(out, (c_kv, k_pe))`` when ``use_cache`` is true.
        """
        b, s, _ = x.shape
        if position_ids.shape != (b, s):
            raise ValueError(
                f"position_ids must be [b, s] = {(b, s)}, got {tuple(position_ids.shape)}"
            )

        # Queries: [b, s, n, d_nope + d_rope]
        q = self._project_q(x).view(b, s, self.num_heads, self.qk_head_dim)
        q_nope, q_pe = torch.split(
            q, [self.qk_nope_head_dim, self.qk_rope_head_dim], dim=-1
        )

        # Cached path: produce the new latent/rope key, append the history.
        kv_a = self.kv_a_proj_with_mqa(x)
        c_kv_new = kv_a[..., : self.kv_lora_rank]
        k_pe_new = kv_a[..., self.kv_lora_rank :].unsqueeze(1)  # [b, 1, s, d_r]

        # RoPE is applied only to the new positions.  Cached k_pe is already
        # rotated; rotating it again would corrupt every past key.
        q_pe = q_pe.transpose(1, 2)  # [b, n, s, d_r]
        q_pe, k_pe_new = self.rotary_emb(q_pe, k_pe_new, position_ids)

        if past_key_value is not None:
            past_c, past_k_pe = past_key_value
            if past_c.size(0) != b:
                raise ValueError("past cache batch size does not match the input")
            if past_c.size(1) != past_k_pe.size(2):
                raise ValueError("past latent and RoPE key lengths disagree")
            q_start = past_c.size(1)
            c_kv = torch.cat([past_c, c_kv_new], dim=1)
            k_pe = torch.cat([past_k_pe, k_pe_new], dim=2)
        else:
            q_start = 0
            c_kv = c_kv_new
            k_pe = k_pe_new

        # Recompute K_nope/V from the full latent.  Training never needs the
        # expanded cache; inference trades a small GEMM for a much smaller
        # cache.  This is the one place where MLA differs from a normal
        # K/V-cache decoder.
        k_nope, v = self._split_kv(c_kv)
        k_nope = k_nope.transpose(1, 2)  # [b, n, s_kv, d_nope]
        v = v.transpose(1, 2)  # [b, n, s_kv, d_v]

        q_nope = q_nope.transpose(1, 2)  # [b, n, s_q, d_nope]
        # Content score [b, n, s_q, s_kv]
        scores = torch.matmul(q_nope, k_nope.transpose(-2, -1))
        # Decoupled RoPE score; k_pe is shared by all heads, so broadcast.
        scores = scores + torch.matmul(q_pe, k_pe.transpose(-2, -1))
        scores = scores.float() * self.scale

        s_q = q.size(1)
        s_kv = c_kv.size(1)
        q_positions = torch.arange(q_start, q_start + s_q, device=x.device)
        kv_positions = torch.arange(s_kv, device=x.device)
        causal = kv_positions.unsqueeze(0) <= q_positions.unsqueeze(1)
        scores = scores.masked_fill(
            ~causal.unsqueeze(0).unsqueeze(0), float("-inf")
        )
        if attention_mask is not None:
            if attention_mask.shape != (b, s_kv):
                raise ValueError(
                    f"attention_mask must be [b, s_kv] = {(b, s_kv)}, "
                    f"got {tuple(attention_mask.shape)}"
                )
            mask = attention_mask[:, None, None, :].to(torch.bool)
            scores = scores.masked_fill(~mask, float("-inf"))

        probs = torch.softmax(scores, dim=-1)
        # Fully masked query rows (all-padding) must produce zeros, not NaN;
        # see the same guard in CoreAttention.
        probs = torch.nan_to_num(probs, nan=0.0).to(v.dtype)
        if self.training and self.attention_dropout > 0:
            probs = F.dropout(probs, p=self.attention_dropout)
        out = torch.matmul(probs, v)  # [b, n, s_q, d_v]
        out = out.transpose(1, 2).contiguous().view(b, s_q, -1)
        out = self.o_proj(out)
        if use_cache:
            return out, (c_kv, k_pe)
        return out

    def extra_repr(self) -> str:
        return (
            f"h={self.hidden_size}, heads={self.num_heads}, "
            f"q_lora={self.q_lora_rank}, kv_lora={self.kv_lora_rank}, "
            f"d_nope={self.qk_nope_head_dim}, d_rope={self.qk_rope_head_dim}, "
            f"d_v={self.v_head_dim}"
        )
