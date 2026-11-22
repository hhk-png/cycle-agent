"""Rotary embeddings, causal attention and tensor-parallel self-attention."""
from __future__ import annotations

import math
from typing import Optional

import torch
import torch.nn as nn
import torch.nn.functional as F
from . import comm
from .layers import ColumnParallelLinear, RowParallelLinear
from .state import (
    _group_rank,
    _group_size,
    get_context_parallel_group,
    get_context_parallel_rank,
    get_context_parallel_world_size,
    get_tensor_model_parallel_world_size,
)


class _GatherContextParallel(torch.autograd.Function):
    @staticmethod
    def forward(ctx, x: torch.Tensor) -> torch.Tensor:
        world = _group_size(get_context_parallel_group())
        ctx.world = world
        if world == 1:
            return x
        x = x.contiguous()
        pieces = [torch.empty_like(x) for _ in range(world)]
        comm.all_gather(pieces, x, group=get_context_parallel_group())
        return torch.cat(pieces, dim=2)

    @staticmethod
    def backward(ctx, grad_output: torch.Tensor):
        if ctx.world == 1:
            return grad_output
        # Every CP rank uses the *same* gathered K/V for a different slice of
        # queries, and the global loss is the sum of those query slices.  The
        # gradient of the broadcast-like gather is therefore the sum of all
        # ranks' grad_outputs, followed by the local chunk.  Chunking without
        # the all-reduce silently drops the K/V gradient contributed by every
        # other query rank.
        grad_output = comm.all_reduce(
            grad_output, op=comm.SUM, group=get_context_parallel_group()
        )
        rank = _group_rank(get_context_parallel_group())
        return torch.chunk(grad_output, ctx.world, dim=2)[rank]


def _gather_cp_sequence(x: torch.Tensor) -> torch.Tensor:
    return _GatherContextParallel.apply(x)


class RotaryEmbedding(nn.Module):
    def __init__(
        self,
        head_dim: int,
        max_seq_len: int,
        base: float = 10000.0,
        interleaved: bool = False,
        device: Optional[torch.device] = None,
    ) -> None:
        super().__init__()
        self.head_dim = head_dim
        self.max_seq_len = max_seq_len
        self.base = base
        self.interleaved = interleaved
        inv_freq = 1.0 / (
            base ** (torch.arange(0, head_dim, 2, dtype=torch.float32, device=device) / head_dim)
        )
        self.register_buffer("inv_freq", inv_freq, persistent=False)
        self._cache_len = 0
        self._cos: Optional[torch.Tensor] = None
        self._sin: Optional[torch.Tensor] = None
        self._build_cache(max_seq_len, device)

    def _build_cache(self, seq_len: int, device: Optional[torch.device] = None) -> None:
        device = device or self.inv_freq.device
        if (
            seq_len <= self._cache_len
            and self._cos is not None
            and self._cos.device == device
        ):
            return
        # Build the full cached length, not just the requested ``seq_len``.
        # When the device changes (CPU prefill -> CUDA decode) the old cache is
        # unusable, but it may already be longer than the current request; the
        # reshape must use the actual number of positions or it fails.
        length = max(seq_len, self._cache_len)
        positions = torch.arange(length, dtype=torch.float32, device=device)
        freqs = torch.outer(positions, self.inv_freq.to(device))
        if self.interleaved:
            emb = torch.stack((freqs, freqs), dim=-1).reshape(length, self.head_dim)
        else:
            emb = torch.cat((freqs, freqs), dim=-1)
        self._cos = emb.cos()
        self._sin = emb.sin()
        self._cache_len = length

    def forward(
        self, q: torch.Tensor, k: torch.Tensor, position_ids: torch.Tensor
    ) -> tuple[torch.Tensor, torch.Tensor]:
        # q: [b, h, s, d], position_ids: [b, s]
        self._build_cache(int(position_ids.max().item()) + 1, q.device)
        assert self._cos is not None and self._sin is not None
        cos = self._cos[position_ids].to(q.dtype).unsqueeze(1)  # [b,1,s,d]
        sin = self._sin[position_ids].to(q.dtype).unsqueeze(1)
        return _apply_rope(q, cos, sin, self.interleaved), _apply_rope(
            k, cos, sin, self.interleaved
        )


def _rotate_half(x: torch.Tensor, interleaved: bool = False) -> torch.Tensor:
    if interleaved:
        x1 = x[..., ::2]
        x2 = x[..., 1::2]
        return torch.stack((-x2, x1), dim=-1).flatten(-2)
    half = x.shape[-1] // 2
    return torch.cat((-x[..., half:], x[..., :half]), dim=-1)


def _apply_rope(
    x: torch.Tensor, cos: torch.Tensor, sin: torch.Tensor, interleaved: bool
) -> torch.Tensor:
    return x * cos + _rotate_half(x, interleaved) * sin


class CoreAttention(nn.Module):
    """Causal scaled dot-product attention.

    It intentionally uses explicit matmuls so the tutorial can show the
    quadratic score matrix and the context-parallel mask.  A production kernel
    would fuse this into FlashAttention / cuDNN attention and never materialise
    the full matrix.
    """

    def __init__(self, dropout: float = 0.0) -> None:
        super().__init__()
        self.dropout_p = dropout

    def forward(
        self,
        q: torch.Tensor,
        k: torch.Tensor,
        v: torch.Tensor,
        *,
        num_query_groups: int,
        q_start: int,
        kv_len: int,
        attention_mask: Optional[torch.Tensor] = None,
    ) -> torch.Tensor:
        # q: [b, hq_local, sq, d], k/v: [b, hkv_local, skv, d]
        num_heads = q.size(1)
        num_kv_heads = k.size(1)
        if num_heads != num_kv_heads:
            repeat = num_heads // num_kv_heads
            k = k.repeat_interleave(repeat, dim=1)
            v = v.repeat_interleave(repeat, dim=1)

        scale = 1.0 / math.sqrt(q.size(-1))
        scores = torch.matmul(q, k.transpose(-2, -1)) * scale
        # Keep the softmax in fp32, as Megatron's fused attention does.
        scores = scores.float()

        sq = q.size(2)
        q_positions = torch.arange(q_start, q_start + sq, device=q.device)
        kv_positions = torch.arange(kv_len, device=q.device)
        causal = kv_positions.unsqueeze(0) <= q_positions.unsqueeze(1)  # [sq, skv]
        scores = scores.masked_fill(~causal.unsqueeze(0).unsqueeze(0), float("-inf"))
        if attention_mask is not None:
            # attention_mask: [b, skv] with 0 for padding.
            mask = attention_mask[:, None, None, :].to(torch.bool)
            scores = scores.masked_fill(~mask, float("-inf"))

        probs = torch.softmax(scores, dim=-1)
        # A fully masked query row (all-padding, or left padding whose visible
        # prefix is empty) has no finite softmax denominator.  Fused attention
        # returns a zero row, not NaN; do the same so one padded query cannot
        # contaminate the residual stream.
        probs = torch.nan_to_num(probs, nan=0.0)
        probs = probs.to(q.dtype)
        if self.training and self.dropout_p > 0:
            probs = F.dropout(probs, p=self.dropout_p)
        out = torch.matmul(probs, v)
        return out


class SelfAttention(nn.Module):
    def __init__(
        self,
        hidden_size: int,
        num_attention_heads: int,
        num_query_groups: int,
        *,
        attention_dropout: float = 0.0,
        add_qkv_bias: bool = False,
        sequence_parallel: bool = False,
        init_method_std: float = 0.02,
        fp8_simulation: bool = False,
        device: Optional[torch.device] = None,
    ) -> None:
        super().__init__()
        world = get_tensor_model_parallel_world_size()
        self.hidden_size = hidden_size
        self.num_attention_heads = num_attention_heads
        self.num_query_groups = num_query_groups
        self.num_attention_heads_per_partition = num_attention_heads // world
        self.num_query_groups_per_partition = num_query_groups // world
        self.head_dim = hidden_size // num_attention_heads
        self.sequence_parallel = sequence_parallel

        q_dim = self.num_attention_heads_per_partition * self.head_dim
        kv_dim = self.num_query_groups_per_partition * self.head_dim
        self.q_proj = ColumnParallelLinear(
            hidden_size,
            q_dim * world,
            bias=add_qkv_bias,
            sequence_parallel=sequence_parallel,
            init_method_std=init_method_std,
            fp8_simulation=fp8_simulation,
            device=device,
        )
        self.k_proj = ColumnParallelLinear(
            hidden_size,
            kv_dim * world,
            bias=add_qkv_bias,
            sequence_parallel=sequence_parallel,
            init_method_std=init_method_std,
            fp8_simulation=fp8_simulation,
            device=device,
        )
        self.v_proj = ColumnParallelLinear(
            hidden_size,
            kv_dim * world,
            bias=add_qkv_bias,
            sequence_parallel=sequence_parallel,
            init_method_std=init_method_std,
            fp8_simulation=fp8_simulation,
            device=device,
        )
        self.out_proj = RowParallelLinear(
            hidden_size,
            hidden_size,
            bias=add_qkv_bias,
            sequence_parallel=sequence_parallel,
            init_method_std=init_method_std,
            fp8_simulation=fp8_simulation,
            device=device,
        )
        self.core_attention = CoreAttention(attention_dropout)
        self.rotary_emb: Optional[RotaryEmbedding] = None

    def set_rotary_embedding(self, rotary_emb: Optional[RotaryEmbedding]) -> None:
        self.rotary_emb = rotary_emb

    def forward(
        self,
        x: torch.Tensor,
        *,
        position_ids: torch.Tensor,
        attention_mask: Optional[torch.Tensor] = None,
        past_key_value: Optional[tuple[torch.Tensor, torch.Tensor]] = None,
        use_cache: bool = False,
    ):
        b = x.size(0)
        q = self.q_proj(x)
        k = self.k_proj(x)
        v = self.v_proj(x)
        # With sequence parallelism the projections first all-gather the
        # sequence, so their length can differ from the input length.
        q_seq = q.size(1)
        q = q.view(b, q_seq, self.num_attention_heads_per_partition, self.head_dim).transpose(1, 2)
        k = k.view(b, q_seq, self.num_query_groups_per_partition, self.head_dim).transpose(1, 2)
        v = v.view(b, q_seq, self.num_query_groups_per_partition, self.head_dim).transpose(1, 2)

        if self.rotary_emb is not None:
            q, k = self.rotary_emb(q, k, position_ids)

        cp_world = get_context_parallel_world_size()
        if cp_world > 1:
            if use_cache:
                raise NotImplementedError(
                    "KV-cache decoding with context parallel is not supported; "
                    "use CP for prefill-only workloads or disable CP for decoding"
                )
            # Gather the full K/V sequence; each rank keeps its local queries.
            k = _gather_cp_sequence(k)
            v = _gather_cp_sequence(v)
            q_start = get_context_parallel_rank() * q_seq
            kv_len = q_seq * cp_world
        else:
            if past_key_value is not None:
                past_k, past_v = past_key_value
                if past_k.size(0) != b or past_k.size(1) != k.size(1):
                    raise ValueError("past K/V batch/head shape does not match the model")
                k = torch.cat((past_k, k), dim=2)
                v = torch.cat((past_v, v), dim=2)
            q_start = 0 if past_key_value is None else past_key_value[0].size(2)
            kv_len = k.size(2)

        out = self.core_attention(
            q,
            k,
            v,
            num_query_groups=self.num_query_groups_per_partition,
            q_start=q_start,
            kv_len=kv_len,
            attention_mask=attention_mask,
        )
        out = out.transpose(1, 2).contiguous().view(b, q_seq, -1)
        out = self.out_proj(out)
        if use_cache:
            return out, (k, v)
        return out
