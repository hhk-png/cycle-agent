"""A compact Mixture-of-Experts layer.

This is *expert-replicated* MoE: every rank owns every expert.  Tensor
parallelism shards each expert's GEMMs exactly like a dense MLP.  Production
Megatron additionally shards experts across ranks (expert parallelism) and
uses all-to-all dispatch; the routing math and capacity logic are the same.

Sequence parallelism is supported by gathering the sequence before routing and
scattering it afterwards.  That keeps routing identical on every TP rank.
"""
from __future__ import annotations

import math
from typing import Tuple

import torch
import torch.nn as nn
import torch.nn.functional as F

from .layers import MLP, _init_replicated_weight
from .state import (
    gather_from_sequence_parallel_region,
    scatter_to_sequence_parallel_region,
)


class TopKRouter(nn.Module):
    def __init__(self, hidden_size: int, num_experts: int, init_std: float = 0.02):
        super().__init__()
        self.weight = nn.Parameter(torch.empty(num_experts, hidden_size))
        _init_replicated_weight(self.weight, init_std)
        self.weight._is_replicated = True

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return F.linear(x, self.weight)


class MoELayer(nn.Module):
    is_moe = True

    def __init__(
        self,
        hidden_size: int,
        ffn_hidden_size: int,
        num_experts: int,
        top_k: int,
        *,
        capacity_factor: float = 1.25,
        activation: str = "swiglu",
        add_bias: bool = False,
        sequence_parallel: bool = False,
        init_method_std: float = 0.02,
        fp8_simulation: bool = False,
        device=None,
    ) -> None:
        super().__init__()
        if top_k < 1 or top_k > num_experts:
            raise ValueError("top_k must be in [1, num_experts]")
        self.hidden_size = hidden_size
        self.num_experts = num_experts
        self.top_k = top_k
        self.capacity_factor = capacity_factor
        self.sequence_parallel = sequence_parallel
        self.router = TopKRouter(hidden_size, num_experts, init_method_std)
        self.experts = nn.ModuleList(
            [
                MLP(
                    hidden_size,
                    ffn_hidden_size,
                    activation=activation,
                    add_bias=add_bias,
                    # We gather once in MoE.forward; experts then operate on the
                    # full sequence and do not gather again.
                    sequence_parallel=False,
                    init_method_std=init_method_std,
                    fp8_simulation=fp8_simulation,
                    device=device,
                )
                for _ in range(num_experts)
            ]
        )

    def forward(self, x: torch.Tensor) -> Tuple[torch.Tensor, torch.Tensor]:
        if self.sequence_parallel:
            x = gather_from_sequence_parallel_region(x)

        shape = x.shape
        flat = x.reshape(-1, shape[-1])
        logits = self.router(flat)
        probs = torch.softmax(logits.float(), dim=-1).to(flat.dtype)
        topk_probs, topk_idx = torch.topk(probs, self.top_k, dim=-1)

        num_tokens = flat.size(0)
        capacity = max(
            1,
            math.ceil(num_tokens * self.top_k / self.num_experts * self.capacity_factor),
        )
        out = torch.zeros_like(flat)
        counts = torch.zeros(self.num_experts, device=flat.device, dtype=flat.dtype)

        for expert_id, expert in enumerate(self.experts):
            mask = topk_idx == expert_id  # [N, k]
            per_token = mask.any(dim=-1)
            token_idx = per_token.nonzero(as_tuple=False).squeeze(1)
            if token_idx.numel() == 0:
                continue
            if token_idx.numel() > capacity:
                # Deterministic overflow policy: keep the first tokens in
                # flattened order.  Switch-style capacity dropping.
                token_idx = token_idx[:capacity]
            counts[expert_id] = token_idx.numel()
            expert_out = expert(flat.index_select(0, token_idx))
            weight = probs.index_select(0, token_idx)[:, expert_id].unsqueeze(-1)
            out.index_add_(0, token_idx, expert_out * weight)

        # Switch Transformer load-balancing loss: E * sum_i(f_i * P_i).
        assignments = torch.bincount(
            topk_idx.reshape(-1), minlength=self.num_experts
        ).to(flat.dtype)
        f_i = assignments / max(1, num_tokens * self.top_k)
        p_i = probs.mean(dim=0)
        aux_loss = self.num_experts * torch.sum(f_i * p_i)

        out = out.reshape(shape)
        if self.sequence_parallel:
            out = scatter_to_sequence_parallel_region(out)
        return out, aux_loss
