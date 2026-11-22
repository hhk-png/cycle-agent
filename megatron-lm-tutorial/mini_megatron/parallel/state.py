"""Process-group construction and collective communication helpers.

Rank layout (the fastest-varying dimension is tensor parallel):

    rank = ((pp_idx * dp_size + dp_idx) * cp_size + cp_idx) * tp_size + tp_idx

This mirrors the important property of Megatron-LM's default layout: tensor
parallel peers are contiguous, pipeline peers are far apart, and data parallel
peers own independent samples.  The layout is intentionally simple; production
Megatron supports configurable order and overlapping expert/data groups.

Communication goes through :mod:`mini_megatron.parallel.comm`, which can use
``torch.distributed`` or a portable queue backend (useful on Windows, where
stock PyTorch wheels may fail to create a Gloo TCP device).
"""
from __future__ import annotations

import os
from typing import List, Optional

import torch

from . import comm

_TP_GROUP = None
_PP_GROUP = None
_CP_GROUP = None
_DP_GROUP = None
_EP_GROUP = None

_TP_SIZE = 1
_PP_SIZE = 1
_CP_SIZE = 1
_DP_SIZE = 1
_EP_SIZE = 1
_INITIALIZED = False


def ensure_divisibility(value: int, divisor: int, name: str) -> int:
    if divisor <= 0:
        raise ValueError(f"{name} divisor must be positive, got {divisor}")
    if value % divisor != 0:
        raise ValueError(f"{value} is not divisible by {divisor} ({name})")
    return value // divisor


def _new_group(ranks: List[int]):
    if not comm.is_initialized():
        return None
    if len(ranks) == 1 and comm.get_world_size() == 1:
        # A one-rank world needs no collective object.
        return None
    return comm.new_group(ranks)


def initialize_model_parallel(
    tensor_model_parallel_size: int = 1,
    pipeline_model_parallel_size: int = 1,
    context_parallel_size: int = 1,
    expert_model_parallel_size: int = 1,
) -> None:
    """Create TP/PP/CP/DP/EP groups for every rank.

    The mini layout shards experts across the *entire* data-parallel dimension
    (``ep == dp``).  This keeps the gradient semantics simple: every expert
    lives on exactly one rank, so expert gradients are not averaged across DP
    replicas.  Production Megatron additionally supports ``ep < dp`` with
    expert replicas and a separate DP reduction group.
    """
    global _TP_GROUP, _PP_GROUP, _CP_GROUP, _DP_GROUP, _EP_GROUP
    global _TP_SIZE, _PP_SIZE, _CP_SIZE, _DP_SIZE, _EP_SIZE, _INITIALIZED

    if not comm.is_initialized():
        _TP_SIZE = tensor_model_parallel_size
        _PP_SIZE = pipeline_model_parallel_size
        _CP_SIZE = context_parallel_size
        _DP_SIZE = 1
        _EP_SIZE = 1
        _INITIALIZED = True
        return

    world_size = comm.get_world_size()
    model_parallel = (
        tensor_model_parallel_size * pipeline_model_parallel_size * context_parallel_size
    )
    if world_size % model_parallel != 0:
        raise ValueError(
            f"world_size={world_size} is not divisible by "
            f"tp*pp*cp={model_parallel}"
        )

    _TP_SIZE = tensor_model_parallel_size
    _PP_SIZE = pipeline_model_parallel_size
    _CP_SIZE = context_parallel_size
    _DP_SIZE = world_size // model_parallel
    _EP_SIZE = expert_model_parallel_size

    if _EP_SIZE < 1:
        raise ValueError("expert_model_parallel_size must be positive")
    if _EP_SIZE not in (1, _DP_SIZE):
        raise ValueError(
            "mini-megatron shards experts across the whole DP dimension: "
            f"expert_model_parallel_size ({_EP_SIZE}) must be 1 or dp_size "
            f"({_DP_SIZE})"
        )

    rank = comm.get_rank()
    tp_idx = rank % _TP_SIZE
    cp_idx = (rank // _TP_SIZE) % _CP_SIZE
    dp_idx = (rank // (_TP_SIZE * _CP_SIZE)) % _DP_SIZE
    pp_idx = rank // (_TP_SIZE * _CP_SIZE * _DP_SIZE)

    for pp in range(_PP_SIZE):
        for dp in range(_DP_SIZE):
            for cp in range(_CP_SIZE):
                base = pp * (_TP_SIZE * _CP_SIZE * _DP_SIZE) + dp * (
                    _TP_SIZE * _CP_SIZE
                ) + cp * _TP_SIZE
                group = _new_group([base + i for i in range(_TP_SIZE)])
                if pp == pp_idx and dp == dp_idx and cp == cp_idx:
                    _TP_GROUP = group

    for pp in range(_PP_SIZE):
        for dp in range(_DP_SIZE):
            for tp in range(_TP_SIZE):
                ranks = [
                    pp * (_TP_SIZE * _CP_SIZE * _DP_SIZE)
                    + dp * (_TP_SIZE * _CP_SIZE)
                    + cp * _TP_SIZE
                    + tp
                    for cp in range(_CP_SIZE)
                ]
                group = _new_group(ranks)
                if pp == pp_idx and dp == dp_idx and tp == tp_idx:
                    _CP_GROUP = group

    for pp in range(_PP_SIZE):
        for cp in range(_CP_SIZE):
            for tp in range(_TP_SIZE):
                ranks = [
                    pp * (_TP_SIZE * _CP_SIZE * _DP_SIZE)
                    + dp * (_TP_SIZE * _CP_SIZE)
                    + cp * _TP_SIZE
                    + tp
                    for dp in range(_DP_SIZE)
                ]
                group = _new_group(ranks)
                if pp == pp_idx and cp == cp_idx and tp == tp_idx:
                    _DP_GROUP = group

    for tp in range(_TP_SIZE):
        for cp in range(_CP_SIZE):
            for dp in range(_DP_SIZE):
                ranks = [
                    pp * (_TP_SIZE * _CP_SIZE * _DP_SIZE)
                    + dp * (_TP_SIZE * _CP_SIZE)
                    + cp * _TP_SIZE
                    + tp
                    for pp in range(_PP_SIZE)
                ]
                group = _new_group(ranks)
                if tp == tp_idx and cp == cp_idx and dp == dp_idx:
                    _PP_GROUP = group

    _EP_GROUP = None
    if _EP_SIZE > 1:
        # Expert-parallel peers share (pp, cp, tp) and differ in dp.  This is
        # the same rank set as the DP group, but a distinct group object keeps
        # the portable backend's message keys separate.
        for pp in range(_PP_SIZE):
            for cp in range(_CP_SIZE):
                for tp in range(_TP_SIZE):
                    ranks = [
                        pp * (_TP_SIZE * _CP_SIZE * _DP_SIZE)
                        + dp * (_TP_SIZE * _CP_SIZE)
                        + cp * _TP_SIZE
                        + tp
                        for dp in range(_EP_SIZE)
                    ]
                    group = _new_group(ranks)
                    if pp == pp_idx and cp == cp_idx and tp == tp_idx:
                        _EP_GROUP = group

    _INITIALIZED = True


def destroy_model_parallel() -> None:
    global _TP_GROUP, _PP_GROUP, _CP_GROUP, _DP_GROUP, _EP_GROUP, _INITIALIZED
    _TP_GROUP = _PP_GROUP = _CP_GROUP = _DP_GROUP = _EP_GROUP = None
    _INITIALIZED = False
    # Avoid leaking the deterministic init stream across tests/runs.  Import
    # locally to keep state.py free of a module-level dependency on layers.py.
    try:
        from .layers import set_model_parallel_init_seed

        set_model_parallel_init_seed(None)
    except Exception:
        pass


def model_parallel_is_initialized() -> bool:
    return _INITIALIZED


def get_tensor_model_parallel_group():
    return _TP_GROUP


def get_pipeline_model_parallel_group():
    return _PP_GROUP


def get_context_parallel_group():
    return _CP_GROUP


def get_data_parallel_group():
    return _DP_GROUP


def get_expert_parallel_group():
    return _EP_GROUP


def get_tensor_model_parallel_world_size() -> int:
    return _TP_SIZE


def get_pipeline_model_parallel_world_size() -> int:
    return _PP_SIZE


def get_context_parallel_world_size() -> int:
    return _CP_SIZE


def get_data_parallel_world_size() -> int:
    return _DP_SIZE


def get_expert_parallel_world_size() -> int:
    return _EP_SIZE


def get_expert_parallel_rank() -> int:
    if _EP_GROUP is None:
        return 0
    return comm.get_group_rank(_EP_GROUP)


def get_tensor_model_parallel_rank() -> int:
    if _TP_GROUP is None:
        return 0
    return comm.get_group_rank(_TP_GROUP)


def get_pipeline_model_parallel_rank() -> int:
    if _PP_GROUP is None:
        return 0
    return comm.get_group_rank(_PP_GROUP)


def get_context_parallel_rank() -> int:
    if _CP_GROUP is None:
        return 0
    return comm.get_group_rank(_CP_GROUP)


def get_data_parallel_rank() -> int:
    if _DP_GROUP is None:
        return 0
    return comm.get_group_rank(_DP_GROUP)


def is_pipeline_first_stage() -> bool:
    return get_pipeline_model_parallel_rank() == 0


def is_pipeline_last_stage() -> bool:
    return get_pipeline_model_parallel_rank() == _PP_SIZE - 1


def get_global_rank() -> int:
    return comm.get_rank()


def get_world_size() -> int:
    return comm.get_world_size()


def get_rank() -> int:
    return comm.get_rank()


def set_random_seed(seed: int) -> None:
    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(seed)


def _group_size(group) -> int:
    if group is None:
        return 1
    return comm.get_group_size(group)


def _group_rank(group) -> int:
    if group is None:
        return 0
    return comm.get_group_rank(group)


def all_reduce(tensor: torch.Tensor, op: str = comm.SUM, group=None) -> torch.Tensor:
    group = group if group is not None else _TP_GROUP
    if _group_size(group) == 1:
        return tensor
    return comm.all_reduce(tensor, op=op, group=group)


def all_gather_last_dim(tensor: torch.Tensor, group=None) -> torch.Tensor:
    group = group if group is not None else _TP_GROUP
    world = _group_size(group)
    if world == 1:
        return tensor
    tensor = tensor.contiguous()
    pieces = [torch.empty_like(tensor) for _ in range(world)]
    comm.all_gather(pieces, tensor, group=group)
    return torch.cat(pieces, dim=-1)


def reduce_scatter_last_dim(tensor: torch.Tensor, group=None) -> torch.Tensor:
    group = group if group is not None else _TP_GROUP
    world = _group_size(group)
    if world == 1:
        return tensor
    last = tensor.size(-1)
    if last % world != 0:
        raise ValueError(f"last dim {last} is not divisible by group size {world}")
    output = torch.empty(
        *tensor.shape[:-1], last // world, dtype=tensor.dtype, device=tensor.device
    )
    comm.reduce_scatter_tensor(output, tensor, op=comm.SUM, group=group)
    return output


def broadcast(tensor: torch.Tensor, src: int = 0, group=None) -> torch.Tensor:
    group = group if group is not None else _TP_GROUP
    if _group_size(group) == 1:
        return tensor
    return comm.broadcast(tensor, src=src, group=group)


def barrier(group=None) -> None:
    comm.barrier(group=group)


def env_rank_world_size() -> tuple[int, int]:
    return int(os.environ.get("RANK", "0")), int(os.environ.get("WORLD_SIZE", "1"))


# Autograd-aware collectives -------------------------------------------------


class _CopyToTensorParallelRegion(torch.autograd.Function):
    @staticmethod
    def forward(ctx, x):
        return x

    @staticmethod
    def backward(ctx, grad_output):
        return grad_output


class _GatherFromTensorParallelRegion(torch.autograd.Function):
    @staticmethod
    def forward(ctx, x):
        ctx.world = _group_size(_TP_GROUP)
        return all_gather_last_dim(x, _TP_GROUP)

    @staticmethod
    def backward(ctx, grad_output):
        return torch.chunk(grad_output, ctx.world, dim=-1)[_group_rank(_TP_GROUP)]


class _ReduceFromTensorParallelRegion(torch.autograd.Function):
    @staticmethod
    def forward(ctx, x):
        return all_reduce(x, group=_TP_GROUP)

    @staticmethod
    def backward(ctx, grad_output):
        return grad_output


class _ScatterToSequenceParallelRegion(torch.autograd.Function):
    @staticmethod
    def forward(ctx, x):
        ctx.world = _group_size(_TP_GROUP)
        return torch.chunk(x, ctx.world, dim=1)[_group_rank(_TP_GROUP)]

    @staticmethod
    def backward(ctx, grad_output):
        return _all_gather_sequence(grad_output, ctx.world)


class _GatherFromSequenceParallelRegion(torch.autograd.Function):
    @staticmethod
    def forward(ctx, x):
        ctx.world = _group_size(_TP_GROUP)
        return _all_gather_sequence(x, ctx.world)

    @staticmethod
    def backward(ctx, grad_output):
        return torch.chunk(grad_output, ctx.world, dim=1)[_group_rank(_TP_GROUP)]


class _ReduceScatterToSequenceParallelRegion(torch.autograd.Function):
    @staticmethod
    def forward(ctx, x):
        ctx.world = _group_size(_TP_GROUP)
        if ctx.world == 1:
            return x
        # Sequence parallel tensors are [batch, seq, hidden].  The reduction is
        # over the tensor-parallel hidden shards; the scatter is over seq.
        # (Megatron stores [seq, batch, hidden], so its native op reduce-
        # scatters dim 0.  We use all-reduce + chunk for clarity; the result is
        # identical, at the cost of materialising the full sequence.)
        reduced = all_reduce(x, group=_TP_GROUP)
        return torch.chunk(reduced, ctx.world, dim=1)[_group_rank(_TP_GROUP)]

    @staticmethod
    def backward(ctx, grad_output):
        return _all_gather_sequence(grad_output, ctx.world)


def _all_gather_sequence(x: torch.Tensor, world: int) -> torch.Tensor:
    if world == 1:
        return x
    x = x.contiguous()
    pieces = [torch.empty_like(x) for _ in range(world)]
    comm.all_gather(pieces, x, group=_TP_GROUP)
    return torch.cat(pieces, dim=1)


def copy_to_tensor_parallel_region(x: torch.Tensor) -> torch.Tensor:
    return _CopyToTensorParallelRegion.apply(x)


def gather_from_tensor_parallel_region(x: torch.Tensor) -> torch.Tensor:
    return _GatherFromTensorParallelRegion.apply(x)


def reduce_from_tensor_parallel_region(x: torch.Tensor) -> torch.Tensor:
    return _ReduceFromTensorParallelRegion.apply(x)


def scatter_to_sequence_parallel_region(x: torch.Tensor) -> torch.Tensor:
    if _group_size(_TP_GROUP) == 1:
        return x
    return _ScatterToSequenceParallelRegion.apply(x)


def gather_from_sequence_parallel_region(x: torch.Tensor) -> torch.Tensor:
    if _group_size(_TP_GROUP) == 1:
        return x
    return _GatherFromSequenceParallelRegion.apply(x)


def reduce_scatter_to_sequence_parallel_region(x: torch.Tensor) -> torch.Tensor:
    if _group_size(_TP_GROUP) == 1:
        return x
    return _ReduceScatterToSequenceParallelRegion.apply(x)


def gather_from_context_parallel_region(x: torch.Tensor, dim: int = 1) -> torch.Tensor:
    """All-gather sequence chunks across the CP group (educational baseline)."""
    world = _group_size(_CP_GROUP)
    if world == 1:
        return x
    x = x.contiguous()
    pieces = [torch.empty_like(x) for _ in range(world)]
    comm.all_gather(pieces, x, group=_CP_GROUP)
    return torch.cat(pieces, dim=dim)


def scatter_to_context_parallel_region(x: torch.Tensor, dim: int = 1) -> torch.Tensor:
    world = _group_size(_CP_GROUP)
    if world == 1:
        return x
    return torch.chunk(x, world, dim=dim)[_group_rank(_CP_GROUP)]
