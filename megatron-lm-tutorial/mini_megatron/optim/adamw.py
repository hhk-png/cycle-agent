"""Optimizer construction, learning-rate schedules and gradient utilities."""
from __future__ import annotations

import math
from typing import Iterable, List, Sequence

import torch
import torch.nn as nn

from ..config import TrainConfig
from ..parallel import comm
from ..parallel.state import (
    get_context_parallel_world_size,
    get_data_parallel_group,
    get_data_parallel_world_size,
    get_expert_parallel_world_size,
)


def _decay_groups(parameters: Iterable[nn.Parameter]):
    decay: List[nn.Parameter] = []
    no_decay: List[nn.Parameter] = []
    seen = set()
    for p in parameters:
        if not p.requires_grad or id(p) in seen:
            continue
        seen.add(id(p))
        if p.ndim <= 1 or p.shape[-1] == 1:
            no_decay.append(p)
        else:
            decay.append(p)
    return decay, no_decay


def build_optimizer(model: nn.Module, train: TrainConfig) -> torch.optim.AdamW:
    decay, no_decay = _decay_groups(model.parameters())
    groups = []
    if decay:
        groups.append({"params": decay, "weight_decay": train.weight_decay})
    if no_decay:
        groups.append({"params": no_decay, "weight_decay": 0.0})
    return torch.optim.AdamW(
        groups,
        lr=train.lr,
        betas=(train.adam_beta1, train.adam_beta2),
        eps=train.adam_eps,
    )


def get_lr(
    step: int,
    *,
    max_steps: int,
    warmup_steps: int,
    base_lr: float,
    min_lr: float,
    style: str,
) -> float:
    if warmup_steps > 0 and step < warmup_steps:
        return base_lr * (step + 1) / warmup_steps
    if style == "constant":
        return base_lr
    if max_steps <= warmup_steps:
        return base_lr
    progress = min(1.0, max(0.0, (step - warmup_steps) / (max_steps - warmup_steps)))
    if style == "cosine":
        coeff = 0.5 * (1.0 + math.cos(math.pi * progress))
        return min_lr + (base_lr - min_lr) * coeff
    if style == "linear":
        return min_lr + (base_lr - min_lr) * (1.0 - progress)
    if style == "inverse_sqrt":
        # Warmup followed by 1/sqrt(step); normalised to start at base_lr.
        return max(min_lr, base_lr * math.sqrt(max(1.0, warmup_steps) / max(1, step)))
    raise ValueError(f"unknown lr style: {style}")


def set_lr(optimizer: torch.optim.Optimizer, lr: float) -> None:
    for group in optimizer.param_groups:
        group["lr"] = lr


def all_reduce_gradients(
    model: nn.Module,
    group=None,
    average: bool = True,
    *,
    skip_expert_parallel: bool = True,
) -> None:
    """Sum (or average) gradients over a replica group.

    Data parallel uses averaging; context parallel uses summing because each CP
    rank owns a different slice of the same sequence.

    With expert parallelism the DP group is also the EP group.  Expert
    parameters are *not* replicas there: each rank owns a different expert, so
    reducing them across DP would mix unrelated gradients.  They are skipped
    for the DP call and included for the CP call (CP peers hold replicas of the
    same expert slice).

    MoE routing is data-dependent, so a parameter can have a gradient on one
    rank and ``None`` on another.  For MoE models we materialise zero gradients
    before the reduction; otherwise ranks would issue different numbers of
    collectives and hang (portable) or mispair messages (NCCL).
    """
    if not comm.is_initialized():
        return
    group = group or get_data_parallel_group()
    if group is None and get_data_parallel_world_size() == 1:
        return
    world = comm.get_group_size(group) if group is not None else get_data_parallel_world_size()
    if world == 1:
        return
    ep = get_expert_parallel_world_size()
    skip_experts = skip_expert_parallel and ep > 1
    # MoE routing is data-dependent: an expert can have a gradient on one rank
    # and None on another.  Skipping the None side would make ranks issue
    # different numbers of collectives (a hang or silently mispaired messages).
    # Materialise zero gradients so every rank participates in the same order.
    has_moe = any(getattr(module, "is_moe", False) for module in model.modules())
    if has_moe:
        for p in model.parameters():
            if p.grad is None and p.requires_grad:
                p.grad = torch.zeros_like(p)
    for p in model.parameters():
        if p.grad is None:
            continue
        if skip_experts and getattr(p, "_is_expert_parallel", False):
            continue
        reduced = comm.all_reduce(p.grad, op=comm.SUM, group=group)
        p.grad.copy_(reduced)
        if average:
            p.grad.div_(world)
    if skip_experts:
        # Every expert receives tokens from all EP ranks, so its gradient is a
        # *sum* of local mean-loss gradients.  Dense parameters are averaged
        # above; divide the expert gradient by EP to express the same global
        # mean, otherwise expert parameters would see an EP-times larger step.
        for p in model.parameters():
            if p.grad is not None and getattr(p, "_is_expert_parallel", False):
                p.grad.div_(ep)


def global_grad_norm(model: nn.Module) -> torch.Tensor:
    """L2 norm over all local grads, reduced over every rank.

    Summing across the whole default group counts TP shards, PP layers and DP
    replicas exactly once per parameter element, which is the quantity used for
    global clipping.
    """
    dense = torch.zeros((), dtype=torch.float32)
    expert = torch.zeros((), dtype=torch.float32)
    from ..parallel.state import get_tensor_model_parallel_world_size

    tp = max(1, get_tensor_model_parallel_world_size())
    ep = max(1, get_expert_parallel_world_size())
    for p in model.parameters():
        if p.grad is None:
            continue
        local = p.grad.detach().float().pow(2).sum()
        if getattr(p, "_is_replicated", False):
            # Replicated across TP: each rank has the same full gradient after
            # the parameter hook, so count it once rather than tp times.
            local = local / tp
        if ep > 1 and getattr(p, "_is_expert_parallel", False):
            expert = expert + local
        else:
            dense = dense + local
    if comm.is_initialized():
        dense = comm.all_reduce(dense, op=comm.SUM)
        expert = comm.all_reduce(expert, op=comm.SUM)
        # Dense replicas exist across DP and CP: divide both out.  EP-sharded
        # experts exist once per CP slice but not once per DP rank, so only CP
        # replicas are divided out.
        dp = max(1, get_data_parallel_world_size())
        cp = max(1, get_context_parallel_world_size())
        dense = dense / (dp * cp)
        expert = expert / (cp if ep > 1 else dp * cp)
    return (dense + expert).sqrt()


def clip_gradients(model: nn.Module, max_norm: float) -> torch.Tensor:
    norm = global_grad_norm(model)
    if max_norm > 0:
        clip_coef = (max_norm / (norm + 1e-6)).clamp(max=1.0)
        for p in model.parameters():
            if p.grad is not None:
                p.grad.mul_(clip_coef)
    return norm


class ZeRO1Optimizer:
    """A readable ZeRO-1: shard optimizer state, not the parameters.

    Every rank still owns a full replica of the parameters and receives
    all-reduced gradients.  Only the owner of a parameter applies the AdamW
    update; the new value is then broadcast to the other DP ranks.  This saves
    roughly (dp-1)/dp of optimizer state (the dominant training memory at long
    context) without introducing reduce-scatter or parameter gathering.
    """

    def __init__(self, model: nn.Module, optimizer: torch.optim.Optimizer, group=None):
        self.model = model
        self.group = group
        self.world = comm.get_group_size(group) if group is not None else 1
        self.rank = comm.get_group_rank(group) if group is not None else 0
        self.all_params = [p for p in model.parameters() if p.requires_grad]
        self.owned = [i for i in range(len(self.all_params)) if i % self.world == self.rank]
        owned_params = [self.all_params[i] for i in self.owned]
        # Rebuild the optimizer over the owned subset while preserving the
        # learning-rate/weight-decay grouping of the original optimizer.
        lr = optimizer.param_groups[0]["lr"]
        betas = optimizer.defaults.get("betas", (0.9, 0.95))
        eps = optimizer.defaults.get("eps", 1e-8)
        decay = []
        no_decay = []
        for p in owned_params:
            # Match ``build_optimizer`` exactly: 1-D parameters and 2-D
            # parameters with a trailing dimension of 1 are not decayed.
            if p.ndim <= 1 or p.shape[-1] == 1:
                no_decay.append(p)
            else:
                decay.append(p)
        groups = []
        if decay:
            # build_optimizer puts the decay group first.  Do not read the
            # last group: that is the no-decay group and its weight_decay is 0.
            groups.append(
                {
                    "params": decay,
                    "weight_decay": optimizer.param_groups[0].get("weight_decay", 0.1),
                }
            )
        if no_decay:
            groups.append({"params": no_decay, "weight_decay": 0.0})
        self.optimizer = torch.optim.AdamW(groups, lr=lr, betas=betas, eps=eps)

    def zero_grad(self, set_to_none: bool = True) -> None:
        self.model.zero_grad(set_to_none=set_to_none)

    def step(self) -> None:
        self.optimizer.step()
        if self.world > 1:
            for i, p in enumerate(self.all_params):
                owner = i % self.world
                src = comm.get_global_rank(self.group, owner)
                comm.broadcast(p.data, src=src, group=self.group)

    def set_lr(self, lr: float) -> None:
        for group in self.optimizer.param_groups:
            group["lr"] = lr

    def state_dict(self) -> dict:
        return {
            "optimizer": self.optimizer.state_dict(),
            "owned": self.owned,
            "world": self.world,
            "rank": self.rank,
        }

    def load_state_dict(self, state: dict) -> None:
        if state.get("world") != self.world:
            raise ValueError("ZeRO checkpoint was written with a different DP size")
        self.optimizer.load_state_dict(state["optimizer"])


class Muon:
    """Minimal Muon update for 2-D parameters (educational, CPU friendly).

    Muon orthogonalises the momentum-smoothed gradient with a few Newton-
    Schulz iterations and applies the resulting update.  Embeddings, norms and
    biases are normally left to AdamW in a hybrid optimizer.
    """

    def __init__(
        self,
        params: Sequence[nn.Parameter],
        lr: float = 0.02,
        momentum: float = 0.95,
        ns_steps: int = 5,
    ) -> None:
        self.params = list(params)
        self.lr = lr
        self.momentum = momentum
        self.ns_steps = ns_steps
        self.buffers = [torch.zeros_like(p) for p in self.params]

    @torch.no_grad()
    def step(self) -> None:
        for p, buf in zip(self.params, self.buffers):
            if p.grad is None:
                continue
            buf.mul_(self.momentum).add_(p.grad)
            if p.ndim != 2:
                p.add_(buf, alpha=-self.lr)
                continue
            x = buf.float()
            x = x / (x.norm() + 1e-7)
            for _ in range(self.ns_steps):
                a = x @ x.T
                x = 1.5 * x - 0.5 * (a @ x)
            p.add_(x.to(p.dtype), alpha=-self.lr)

    def zero_grad(self) -> None:
        for p in self.params:
            p.grad = None
