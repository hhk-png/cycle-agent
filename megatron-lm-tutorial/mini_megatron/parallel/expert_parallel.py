"""Expert-parallel Mixture-of-Experts with real all-to-all dispatch.

The baseline :class:`~mini_megatron.parallel.moe.MoELayer` keeps a copy of
every expert on every rank (expert replication) and only tensor-parallelises
each expert's GEMMs.  Production Megatron instead shards the experts across an
*expert-parallel* (EP) group and moves tokens with all-to-all:

    rank 0: experts 0 .. E/ep-1
    rank 1: experts E/ep .. 2E/ep-1
    ...
    route -> dispatch (all-to-all) -> local expert GEMM -> combine (all-to-all)

This module implements that path for the mini layout, where the EP group is the
whole data-parallel dimension (``ep == dp``).  The implementation is deliberately
explicit about the two things that make EP hard:

* **Variable-size messages.**  Each rank sends a different number of tokens to
  each peer.  The mini backend pads every peer block to a common maximum and
  truncates after the exchange.  Production uses split sizes and grouped GEMMs.
* **Autograd through the permutation.**  Dispatch and combine are custom
  autograd functions; the backward pass is the reverse all-to-all.  Token
  indices and expert ids travel as non-differentiable metadata.

The class is a drop-in replacement for ``MoELayer`` inside ``TransformerBlock``
when ``parallel.expert_model_parallel_size > 1``.

Known limitation: auxiliary-loss statistics are reduced over the EP group but
not over the CP group, so CP+MoE trains with a per-sequence-slice aux.  The
production implementation reduces counts/probabilities across both.
"""
from __future__ import annotations

import math
from typing import List, Optional, Tuple

import torch
import torch.nn as nn
import torch.nn.functional as F

from . import comm
from .layers import MLP
from .moe import TopKRouter
from .state import (
    gather_from_sequence_parallel_region,
    get_expert_parallel_group,
    get_expert_parallel_rank,
    get_expert_parallel_world_size,
    get_tensor_model_parallel_rank,
    scatter_to_sequence_parallel_region,
)


class _AllToAllRows(torch.autograd.Function):
    """All-to-all over equally shaped row blocks; backward is the reverse."""

    @staticmethod
    def forward(ctx, group, *blocks: torch.Tensor):
        world = comm.get_group_size(group)
        if len(blocks) != world:
            raise ValueError("expected one block per EP rank")
        outputs = [torch.empty_like(block) for block in blocks]
        comm.all_to_all(outputs, blocks, group=group)
        ctx.group = group
        return tuple(outputs)

    @staticmethod
    def backward(ctx, *grad_outputs):
        outputs = [torch.empty_like(grad) for grad in grad_outputs]
        comm.all_to_all(outputs, [grad.contiguous() for grad in grad_outputs], group=ctx.group)
        return (None, *outputs)


class _AllReduceSum(torch.autograd.Function):
    """Forward all-reduce for globally replicated statistics.

    The value is computed independently on every rank (a gather of local
    contributions).  Each rank differentiates only its own contribution, so
    the backward pass is the identity; an all-reduce here would double-count.
    """

    @staticmethod
    def forward(ctx, tensor: torch.Tensor, group):
        ctx.group = group
        return comm.all_reduce(tensor, op=comm.SUM, group=group)

    @staticmethod
    def backward(ctx, grad_output):
        # The forward is a *sum* of local probabilities replicated on every
        # rank.  Each rank's aux loss differentiates the same global value, so
        # the gradient w.r.t. its own local contribution must receive the sum
        # of all ranks' upstream gradients.  Without this, the aux gradient is
        # under-scaled by EP and the router learns too slowly from balancing.
        return comm.all_reduce(grad_output, op=comm.SUM, group=ctx.group), None


def _pad_rows(tensor: torch.Tensor, rows: int) -> torch.Tensor:
    if tensor.size(0) == rows:
        return tensor
    padded = tensor.new_zeros((rows,) + tuple(tensor.shape[1:]))
    padded[: tensor.size(0)] = tensor
    return padded


def _all_to_all_blocks(blocks: List[torch.Tensor], group) -> List[torch.Tensor]:
    outputs = [torch.empty_like(block) for block in blocks]
    comm.all_to_all(outputs, [block.contiguous() for block in blocks], group=group)
    return outputs


class ExpertParallelMoELayer(nn.Module):
    """Top-k MoE whose experts are sharded across the EP group.

    Routing is replicated on every rank, so all EP peers agree on the
    assignment before dispatch.  Each rank owns ``num_experts // ep_size``
    experts; their MLPs are still tensor-parallel (gate/up column, down row).
    """

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
        layer_index: int = 0,
    ) -> None:
        super().__init__()
        ep_size = get_expert_parallel_world_size()
        if ep_size <= 1:
            raise ValueError(
                "ExpertParallelMoELayer requires expert_model_parallel_size > 1"
            )
        if num_experts % ep_size != 0:
            raise ValueError("num_experts must be divisible by EP size")
        if not 1 <= top_k <= num_experts:
            raise ValueError("top_k must be in [1, num_experts]")

        self.hidden_size = hidden_size
        self.ffn_hidden_size = ffn_hidden_size
        self.num_experts = num_experts
        self.top_k = top_k
        self.capacity_factor = capacity_factor
        self.sequence_parallel = sequence_parallel
        self.ep_size = ep_size
        self.ep_rank = get_expert_parallel_rank()
        self.experts_per_rank = num_experts // ep_size
        self.init_method_std = init_method_std
        self.layer_index = int(layer_index)

        # The router is replicated, exactly as in the baseline MoE.  Its
        # initialiser deliberately omits the TP rank offset.
        self.router = TopKRouter(hidden_size, num_experts, init_method_std)
        self.experts = nn.ModuleList(
            [
                MLP(
                    hidden_size,
                    ffn_hidden_size,
                    activation=activation,
                    add_bias=add_bias,
                    sequence_parallel=False,
                    init_method_std=init_method_std,
                    fp8_simulation=fp8_simulation,
                    device=device,
                )
                for _ in range(self.experts_per_rank)
            ]
        )
        self._mark_expert_parameters()
        self._reinit_experts_for_ep_rank()

    # ------------------------------------------------------------------
    # Construction helpers
    # ------------------------------------------------------------------
    def _mark_expert_parameters(self) -> None:
        # The DP gradient all-reduce must skip these: each expert exists on
        # exactly one EP rank, so summing across the DP/EP group would mix
        # gradients of different experts.
        for expert in self.experts:
            for parameter in expert.parameters():
                parameter._is_expert_parallel = True

    def _reinit_experts_for_ep_rank(self) -> None:
        """Give every *global* expert a distinct, reproducible initialisation.

        Without the global-expert offset, local expert 0 on rank 0 and local
        expert 0 on rank 1 would receive the same weights because they consume
        the same model-parallel init stream.  The offset mirrors the TP offset
        used by the sharded layers, so a TP shard is still distinct.
        """
        base = torch.initial_seed()
        tp_rank = get_tensor_model_parallel_rank()
        for local_expert, expert in enumerate(self.experts):
            global_expert = self.ep_rank * self.experts_per_rank + local_expert
            generator = torch.Generator(device=expert_parameters_device(expert))
            generator.manual_seed(
                base
                + 104729 * (global_expert + 1)
                + 10007 * (tp_rank + 1)
                + 7919 * (self.layer_index + 1)
            )
            for parameter in expert.parameters():
                if parameter.ndim >= 2:
                    nn.init.normal_(
                        parameter, mean=0.0, std=self.init_method_std, generator=generator
                    )
                else:
                    nn.init.zeros_(parameter)

    # ------------------------------------------------------------------
    # Forward
    # ------------------------------------------------------------------
    def forward(self, x: torch.Tensor) -> Tuple[torch.Tensor, torch.Tensor]:
        if self.sequence_parallel:
            x = gather_from_sequence_parallel_region(x)

        shape = x.shape
        flat = x.reshape(-1, shape[-1])
        num_tokens = flat.size(0)
        dtype = flat.dtype
        group = get_expert_parallel_group()
        if group is None:
            raise RuntimeError("expert-parallel group is not initialised")

        logits = self.router(flat)
        probs = torch.softmax(logits.float(), dim=-1).to(dtype)
        _, topk_idx = torch.topk(probs, self.top_k, dim=-1)

        # One row per (token, selected expert) pair.  Sorting by owner makes
        # each peer's payload contiguous; the sort is stable so the order is
        # reproducible.
        pair_token = (
            torch.arange(num_tokens, device=flat.device)
            .repeat_interleave(self.top_k)
        )
        pair_expert = topk_idx.reshape(-1)
        pair_weight = probs.gather(1, topk_idx).reshape(-1)
        owner = torch.div(
            pair_expert, self.experts_per_rank, rounding_mode="floor"
        )

        order = torch.argsort(owner, stable=True)
        pair_token = pair_token[order]
        pair_expert = pair_expert[order]
        pair_weight = pair_weight[order]
        owner = owner[order]

        # Send counts and the matching receive counts.  all_gather is small
        # (one integer per peer) and keeps the data path simple.
        send_counts = torch.bincount(owner, minlength=self.ep_size).to(torch.long)
        counts_all = [torch.empty_like(send_counts) for _ in range(self.ep_size)]
        comm.all_gather(counts_all, send_counts, group=group)
        recv_counts = torch.stack(
            [counts_all[src][self.ep_rank] for src in range(self.ep_size)]
        ).to(torch.long)
        max_rows = int(
            comm.all_reduce(
                send_counts.max().reshape(1), op=comm.MAX, group=group
            ).item()
        )
        max_rows = max(1, max_rows)

        # Dispatch: pack [hidden, weight] so the routing weight travels with
        # its token through the same autograd path.  Token ids and local expert
        # ids are metadata and need no gradient.
        packed = flat.new_zeros((pair_expert.numel(), self.hidden_size + 1))
        packed[:, : self.hidden_size] = flat[pair_token]
        packed[:, self.hidden_size] = pair_weight
        metadata = torch.zeros(
            (pair_expert.numel(), 2), dtype=torch.long, device=flat.device
        )
        metadata[:, 0] = pair_token
        metadata[:, 1] = pair_expert - owner * self.experts_per_rank

        dispatch_blocks: List[torch.Tensor] = []
        metadata_blocks: List[torch.Tensor] = []
        offset = 0
        for destination in range(self.ep_size):
            count = int(send_counts[destination].item())
            dispatch_blocks.append(
                _pad_rows(packed[offset : offset + count], max_rows)
            )
            metadata_blocks.append(
                _pad_rows(metadata[offset : offset + count], max_rows)
            )
            offset += count

        received = _AllToAllRows.apply(group, *dispatch_blocks)
        received_meta = _all_to_all_blocks(metadata_blocks, group)
        recv_x = torch.cat(
            [
                received[src][: int(recv_counts[src].item())]
                for src in range(self.ep_size)
            ],
            dim=0,
        )
        recv_meta = torch.cat(
            [
                received_meta[src][: int(recv_counts[src].item())]
                for src in range(self.ep_size)
            ],
            dim=0,
        )
        recv_token = recv_meta[:, 0]
        recv_expert = recv_meta[:, 1]
        recv_weight = recv_x[:, self.hidden_size :]
        recv_hidden = recv_x[:, : self.hidden_size]

        # Global capacity: every expert sees tokens from all EP ranks.
        capacity = max(
            1,
            math.ceil(
                num_tokens
                * self.ep_size
                * self.top_k
                / self.num_experts
                * self.capacity_factor
            ),
        )
        expert_out = recv_hidden.new_zeros(recv_hidden.shape)
        for local_expert, expert in enumerate(self.experts):
            selected = (recv_expert == local_expert).nonzero(as_tuple=False).squeeze(1)
            if selected.numel() == 0:
                continue
            # Deterministic overflow: keep the lowest token ids.
            selected = selected[torch.argsort(recv_token[selected], stable=True)]
            if selected.numel() > capacity:
                selected = selected[:capacity]
            out = expert(recv_hidden.index_select(0, selected))
            expert_out = expert_out.index_copy(
                0, selected, out * recv_weight.index_select(0, selected).to(out.dtype)
            )

        # Combine: send each expert's result back to the rank that owns the
        # token.  The reverse all-to-all is created by autograd.
        combine_blocks: List[torch.Tensor] = []
        combine_meta: List[torch.Tensor] = []
        offset = 0
        for source in range(self.ep_size):
            count = int(recv_counts[source].item())
            combine_blocks.append(
                _pad_rows(expert_out[offset : offset + count], max_rows)
            )
            combine_meta.append(
                _pad_rows(recv_token[offset : offset + count].unsqueeze(1), max_rows)
            )
            offset += count

        returned = _AllToAllRows.apply(group, *combine_blocks)
        returned_meta = _all_to_all_blocks(combine_meta, group)
        # Block ``src`` comes back from rank ``src`` and contains the rows that
        # *this* rank sent to ``src``: truncate by counts_all[this_rank][src].
        returned_x = torch.cat(
            [
                returned[src][: int(counts_all[self.ep_rank][src].item())]
                for src in range(self.ep_size)
            ],
            dim=0,
        )
        returned_token = torch.cat(
            [
                returned_meta[src][
                    : int(counts_all[self.ep_rank][src].item())
                ][:, 0]
                for src in range(self.ep_size)
            ],
            dim=0,
        )
        out = flat.new_zeros((num_tokens, self.hidden_size))
        out = out.index_add(0, returned_token, returned_x)

        # Switch-style auxiliary loss over the *global* assignment statistics.
        assignments = torch.bincount(
            pair_expert, minlength=self.num_experts
        ).to(torch.float32)
        assignments_global = comm.all_reduce(
            assignments, op=comm.SUM, group=group
        )
        f_i = assignments_global / max(1, num_tokens * self.ep_size * self.top_k)
        p_i = _AllReduceSum.apply(probs.mean(dim=0).float(), group) / self.ep_size
        aux_loss = self.num_experts * torch.sum(f_i * p_i).to(dtype)

        out = out.reshape(shape)
        if self.sequence_parallel:
            out = scatter_to_sequence_parallel_region(out)
        return out, aux_loss


def expert_parameters_device(expert: nn.Module) -> torch.device:
    for parameter in expert.parameters():
        return parameter.device
    return torch.device("cpu")
