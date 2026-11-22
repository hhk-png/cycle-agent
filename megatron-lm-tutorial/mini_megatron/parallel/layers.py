"""Tensor-parallel building blocks.

The layers deliberately mirror the names used by Megatron-LM:

* ``ColumnParallelLinear``  shards the output feature dimension.
* ``RowParallelLinear``     shards the input feature dimension and reduces.
* ``VocabParallelEmbedding`` shards the vocabulary.

Sequence-parallel handling is opt-in.  The invariant is simple: with
``sequence_parallel=True`` the hidden state is sharded on the sequence axis
between blocks; column-parallel layers all-gather it, row-parallel layers
reduce-scatter it back.
"""
from __future__ import annotations

import math
from typing import Optional

import torch
import torch.nn as nn
import torch.nn.functional as F

from .state import (
    all_reduce,
    gather_from_sequence_parallel_region,
    gather_from_tensor_parallel_region,
    get_pipeline_model_parallel_rank,
    get_pipeline_model_parallel_world_size,
    get_tensor_model_parallel_group,
    get_tensor_model_parallel_rank,
    get_tensor_model_parallel_world_size,
    reduce_from_tensor_parallel_region,
    reduce_scatter_to_sequence_parallel_region,
    scatter_to_sequence_parallel_region,
)


def divide(numerator: int, denominator: int) -> int:
    if numerator % denominator != 0:
        raise ValueError(f"{numerator} is not divisible by {denominator}")
    return numerator // denominator


# ---------------------------------------------------------------------------
# Model-parallel parameter initialisation
# ---------------------------------------------------------------------------
# Every TP rank owns a *different* slice of a sharded weight.  Seeding all
# ranks identically therefore makes the shards identical, i.e. the full matrix
# is tiled instead of split.  Production Megatron solves this with the
# model-parallel RNG tracker; the mini build uses a small deterministic
# counter so the tutorial can still run without the full tracker.
_INIT_SEED: Optional[int] = None
_INIT_COUNTER = 0

_SEED_TP_STRIDE = 10007
_SEED_PP_STRIDE = 20011
_SEED_LAYER_STRIDE = 7919


def set_model_parallel_init_seed(seed: Optional[int]) -> None:
    """Reset the deterministic initialisation stream.

    Call this immediately before constructing a model.  Sharded layers then
    consume a stream offset by TP rank (different slice) and PP rank (different
    layer); replicated layers omit the TP offset so TP peers stay identical.
    """
    global _INIT_SEED, _INIT_COUNTER
    _INIT_SEED = seed
    _INIT_COUNTER = 0


def _next_init_generator(device, *, sharded: bool) -> Optional[torch.Generator]:
    global _INIT_COUNTER
    if _INIT_SEED is None:
        return None
    tp_world = get_tensor_model_parallel_world_size()
    pp_world = get_pipeline_model_parallel_world_size()
    if tp_world <= 1 and pp_world <= 1:
        # Preserve the historical single-rank path (and the example losses in
        # the tutorial) when there is no model parallelism to disambiguate.
        return None
    seed = _INIT_SEED + _SEED_PP_STRIDE * get_pipeline_model_parallel_rank()
    if sharded:
        seed += _SEED_TP_STRIDE * (get_tensor_model_parallel_rank() + 1)
    seed += _SEED_LAYER_STRIDE * _INIT_COUNTER
    _INIT_COUNTER += 1
    generator = torch.Generator(device=device)
    generator.manual_seed(seed)
    return generator


def _init_sharded_weight(tensor: torch.Tensor, std: float) -> None:
    generator = _next_init_generator(tensor.device, sharded=True)
    nn.init.normal_(tensor, mean=0.0, std=std, generator=generator)


def _init_replicated_weight(tensor: torch.Tensor, std: float) -> None:
    generator = _next_init_generator(tensor.device, sharded=False)
    nn.init.normal_(tensor, mean=0.0, std=std, generator=generator)


def _round_to_fp8(x: torch.Tensor, scale: torch.Tensor) -> torch.Tensor:
    """Quantise forward, but pass the gradient through unchanged (STE).

    A naive ``x.to(float8).to(x.dtype)`` makes the backward quantise the
    upstream gradient through E4M3 as well.  With the divide-by-``scale`` after
    the cast, small gradients (e.g. a weight gradient of ~1e-5) underflow to
    zero, and FP8 simulation silently kills training.  The straight-through
    estimator keeps the forward error (which is what we want to study) while
    leaving the backward mathematically useful.
    """
    quantised = (
        (x * scale).clamp(-448.0, 448.0).to(torch.float8_e4m3fn).to(x.dtype)
    ) / scale
    return x + (quantised - x).detach()


def simulate_fp8(x: torch.Tensor, dim: Optional[int] = None) -> torch.Tensor:
    """Round a tensor through E4M3 with a per-tensor/per-row scale.

    This is a *numerical simulation*, not a speed optimisation.  It lets the
    tutorial demonstrate scaling, clipping and the accuracy loss of FP8 on a
    CPU.  Real Megatron delegates this to Transformer Engine kernels.

    The backward pass uses a straight-through estimator (see
    :func:`_round_to_fp8`): the forward value is quantised, the gradient is
    not.  This is the standard teaching compromise; production FP8 kernels
    have their own (hardware-specific) gradient semantics.
    """
    if not x.is_floating_point():
        return x
    if dim is None:
        amax = x.detach().abs().max().clamp_min(1e-12)
        scale = 448.0 / amax
        return _round_to_fp8(x, scale)
    amax = x.detach().abs().amax(dim=dim, keepdim=True).clamp_min(1e-12)
    scale = 448.0 / amax
    return _round_to_fp8(x, scale)



class _ColumnParallelLinearFn(torch.autograd.Function):
    """Local GEMM + all-reduce of the input gradient.

    A column-parallel layer shards the output feature dimension.  Each rank
    therefore computes only a partial contribution to the input gradient;
    summing those contributions across TP is mandatory.  Forgetting this
    all-reduce yields a model whose forward loss is correct but whose gradients
    (and therefore training) are silently wrong.
    """

    @staticmethod
    def forward(ctx, x, weight, bias, gather_output: bool, group):
        ctx.save_for_backward(x, weight, bias)
        ctx.gather_output = gather_output
        ctx.group = group
        ctx.world = get_tensor_model_parallel_world_size()
        out = F.linear(x, weight, bias)
        if gather_output and ctx.world > 1:
            out = gather_from_tensor_parallel_region(out)
        return out

    @staticmethod
    def backward(ctx, grad_output):
        x, weight, bias = ctx.saved_tensors
        grad_input = grad_weight = grad_bias = None
        if ctx.gather_output and ctx.world > 1:
            grad_output = torch.chunk(grad_output, ctx.world, dim=-1)[
                get_tensor_model_parallel_rank()
            ]
        # Under autocast grad_output can be bf16 while the master weights and
        # the saved activation are fp32.  Do the GEMMs in the weight dtype and
        # cast the input gradient back to the activation dtype.
        work_dtype = weight.dtype
        grad_output_w = grad_output.to(work_dtype)
        if ctx.needs_input_grad[0]:
            grad_input = grad_output_w.matmul(weight).to(x.dtype)
            if ctx.world > 1:
                grad_input = all_reduce(grad_input, group=ctx.group)
        if ctx.needs_input_grad[1]:
            grad_weight = grad_output_w.reshape(-1, grad_output_w.size(-1)).T.matmul(
                x.reshape(-1, x.size(-1)).to(work_dtype)
            )
        if bias is not None and ctx.needs_input_grad[2]:
            grad_bias = grad_output_w.reshape(-1, grad_output_w.size(-1)).sum(dim=0)
        return grad_input, grad_weight, grad_bias, None, None

class ColumnParallelLinear(nn.Module):
    def __init__(
        self,
        input_size: int,
        output_size: int,
        *,
        bias: bool = False,
        gather_output: bool = False,
        sequence_parallel: bool = False,
        init_method_std: float = 0.02,
        fp8_simulation: bool = False,
        device: Optional[torch.device] = None,
    ) -> None:
        super().__init__()
        world = get_tensor_model_parallel_world_size()
        self.input_size = input_size
        self.output_size = output_size
        self.output_size_per_partition = divide(output_size, world)
        self.gather_output = gather_output
        self.sequence_parallel = sequence_parallel
        self.fp8_simulation = fp8_simulation
        self.world_size = world

        self.weight = nn.Parameter(
            torch.empty(self.output_size_per_partition, input_size, device=device)
        )
        _init_sharded_weight(self.weight, init_method_std)
        if bias:
            self.bias = nn.Parameter(
                torch.empty(self.output_size_per_partition, device=device)
            )
            nn.init.zeros_(self.bias)
        else:
            self.register_parameter("bias", None)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        if self.sequence_parallel:
            x = gather_from_sequence_parallel_region(x)
        x_use = simulate_fp8(x) if self.fp8_simulation else x
        w = simulate_fp8(self.weight) if self.fp8_simulation else self.weight
        return _ColumnParallelLinearFn.apply(
            x_use,
            w,
            self.bias,
            self.gather_output,
            get_tensor_model_parallel_group(),
        )

    def extra_repr(self) -> str:
        return (
            f"in={self.input_size}, out={self.output_size}, "
            f"local_out={self.output_size_per_partition}, "
            f"gather_output={self.gather_output}, sp={self.sequence_parallel}"
        )


class RowParallelLinear(nn.Module):
    def __init__(
        self,
        input_size: int,
        output_size: int,
        *,
        bias: bool = False,
        input_is_parallel: bool = True,
        sequence_parallel: bool = False,
        init_method_std: float = 0.02,
        fp8_simulation: bool = False,
        device: Optional[torch.device] = None,
    ) -> None:
        super().__init__()
        world = get_tensor_model_parallel_world_size()
        self.input_size = input_size
        self.output_size = output_size
        self.input_size_per_partition = divide(input_size, world)
        self.input_is_parallel = input_is_parallel
        self.sequence_parallel = sequence_parallel
        self.fp8_simulation = fp8_simulation
        self.world_size = world

        self.weight = nn.Parameter(
            torch.empty(output_size, self.input_size_per_partition, device=device)
        )
        _init_sharded_weight(self.weight, init_method_std)
        if bias:
            self.bias = nn.Parameter(torch.empty(output_size, device=device))
            nn.init.zeros_(self.bias)
            # A row-parallel bias is replicated across TP ranks.
            self.bias._is_replicated = True
            if sequence_parallel and world > 1:
                # The bias is replicated and each TP rank contributes the
                # gradient from its own sequence shard.
                self.bias.register_hook(
                    lambda grad: all_reduce(
                        grad, group=get_tensor_model_parallel_group()
                    )
                )
        else:
            self.register_parameter("bias", None)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        if not self.input_is_parallel and self.world_size > 1:
            x = torch.chunk(x, self.world_size, dim=-1)[get_tensor_model_parallel_rank()]
        x_use = simulate_fp8(x) if self.fp8_simulation else x
        w = simulate_fp8(self.weight) if self.fp8_simulation else self.weight
        out = F.linear(x_use, w, None)
        if self.sequence_parallel:
            out = reduce_scatter_to_sequence_parallel_region(out)
        elif self.world_size > 1:
            out = reduce_from_tensor_parallel_region(out)
        if self.bias is not None:
            out = out + self.bias
        return out

    def extra_repr(self) -> str:
        return (
            f"in={self.input_size}, out={self.output_size}, "
            f"local_in={self.input_size_per_partition}, sp={self.sequence_parallel}"
        )


class VocabParallelEmbedding(nn.Module):
    def __init__(
        self,
        num_embeddings: int,
        embedding_dim: int,
        *,
        init_method_std: float = 0.02,
        sequence_parallel: bool = False,
        device: Optional[torch.device] = None,
    ) -> None:
        super().__init__()
        self.num_embeddings = num_embeddings
        self.embedding_dim = embedding_dim
        self.sequence_parallel = sequence_parallel
        world = get_tensor_model_parallel_world_size()
        self.num_embeddings_per_partition = divide(num_embeddings, world)
        self.vocab_start_index = get_tensor_model_parallel_rank() * (
            self.num_embeddings_per_partition
        )
        self.vocab_end_index = self.vocab_start_index + self.num_embeddings_per_partition
        self.weight = nn.Parameter(
            torch.empty(self.num_embeddings_per_partition, embedding_dim, device=device)
        )
        _init_sharded_weight(self.weight, init_method_std)

    def forward(self, input_ids: torch.Tensor) -> torch.Tensor:
        if input_ids.dtype not in (torch.long, torch.int):
            raise TypeError("input_ids must be integer tokens")
        mask = (input_ids >= self.vocab_start_index) & (input_ids < self.vocab_end_index)
        local_ids = (input_ids - self.vocab_start_index).masked_fill(~mask, 0)
        out = F.embedding(local_ids, self.weight)
        out = out * mask.unsqueeze(-1).to(out.dtype)
        if self.num_embeddings_per_partition * get_tensor_model_parallel_world_size() > 1:
            out = reduce_from_tensor_parallel_region(out)
        if self.sequence_parallel:
            out = scatter_to_sequence_parallel_region(out)
        return out


class LearnedPositionEmbedding(nn.Module):
    """Replicated absolute position embedding (max_seq, hidden)."""

    def __init__(
        self,
        max_seq_len: int,
        hidden_size: int,
        *,
        init_method_std: float = 0.02,
        device: Optional[torch.device] = None,
    ) -> None:
        super().__init__()
        self.weight = nn.Parameter(torch.empty(max_seq_len, hidden_size, device=device))
        _init_replicated_weight(self.weight, init_method_std)
        self.weight._is_replicated = True

    def forward(self, position_ids: torch.Tensor) -> torch.Tensor:
        return F.embedding(position_ids, self.weight)


class MLP(nn.Module):
    """Transformer feed-forward network with optional SwiGLU gating.

    Gate and up projections use *separate* column-parallel layers.  This is one
    extra kernel compared with a fused gate/up projection, but it keeps the
    tensor-parallel layout easy to reason about and numerically identical.
    """

    def __init__(
        self,
        hidden_size: int,
        ffn_hidden_size: int,
        *,
        activation: str = "swiglu",
        add_bias: bool = False,
        sequence_parallel: bool = False,
        init_method_std: float = 0.02,
        fp8_simulation: bool = False,
        device: Optional[torch.device] = None,
    ) -> None:
        super().__init__()
        self.activation = activation
        self.sequence_parallel = sequence_parallel
        if activation == "swiglu":
            self.gate_proj = ColumnParallelLinear(
                hidden_size,
                ffn_hidden_size,
                bias=add_bias,
                sequence_parallel=sequence_parallel,
                init_method_std=init_method_std,
                fp8_simulation=fp8_simulation,
                device=device,
            )
            self.up_proj = ColumnParallelLinear(
                hidden_size,
                ffn_hidden_size,
                bias=add_bias,
                # Both projections must see the same full-sequence layout.
                # A production implementation would all-gather once and feed
                # both GEMMs from the gathered activation.
                sequence_parallel=sequence_parallel,
                init_method_std=init_method_std,
                fp8_simulation=fp8_simulation,
                device=device,
            )
            self.down_proj = RowParallelLinear(
                ffn_hidden_size,
                hidden_size,
                bias=add_bias,
                sequence_parallel=sequence_parallel,
                init_method_std=init_method_std,
                fp8_simulation=fp8_simulation,
                device=device,
            )
        elif activation == "gelu":
            self.fc1 = ColumnParallelLinear(
                hidden_size,
                ffn_hidden_size,
                bias=add_bias,
                sequence_parallel=sequence_parallel,
                init_method_std=init_method_std,
                fp8_simulation=fp8_simulation,
                device=device,
            )
            self.fc2 = RowParallelLinear(
                ffn_hidden_size,
                hidden_size,
                bias=add_bias,
                sequence_parallel=sequence_parallel,
                init_method_std=init_method_std,
                fp8_simulation=fp8_simulation,
                device=device,
            )
        else:
            raise ValueError(f"unsupported activation: {activation}")

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        if self.activation == "swiglu":
            # gate_proj performs the sequence all-gather; up_proj sees the same
            # full-sequence hidden state.
            gate = F.silu(self.gate_proj(x))
            up = self.up_proj(x)
            return self.down_proj(gate * up)
        return self.fc2(F.gelu(self.fc1(x), approximate="tanh"))


class SwiGLUMLP(MLP):
    """Backwards-compatible alias used in older examples."""
