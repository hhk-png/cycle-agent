"""A flat-bucket distributed optimizer (ZeRO-1 with reduce-scatter/all-gather).

The production Megatron ``DistributedOptimizer`` does three things that the
readable ``ZeRO1Optimizer`` in :mod:`mini_megatron.optim.adamw` does not:

1. it flattens every parameter into a small number of contiguous buckets;
2. it uses ``reduce-scatter`` (not all-reduce + broadcast) so each DP rank
   receives only its own gradient shard;
3. it keeps the optimizer state for that shard only, and ``all-gather``s the
   updated parameters once per step.

This module implements those three ideas in one bucket.  It is deliberately
small enough to audit: the flat layout, the padding rule, the AdamW update and
the checkpoint contract are all visible in one file.

Semantics
---------
For ``dp`` ranks, parameter ``p`` at flat offset ``[o, o+n)`` belongs to rank
``r`` iff its flat index falls in ``[r*shard, (r+1)*shard)``.  The split is by
element, not by tensor, so a single matrix can be owned by several ranks.  That
is why the optimizer update is elementwise and does not need the original
tensor boundaries.

The local gradient shard is the *sum* over DP ranks divided by ``dp`` (the
mean).  The global gradient norm is the L2 norm of the concatenation of all
shards, so it is computed by one scalar all-reduce over the local shard norm.
"""
from __future__ import annotations

import math
from typing import Dict, List, Optional

import torch

from ..parallel import comm
from ..parallel.state import (
    get_context_parallel_world_size,
    get_tensor_model_parallel_world_size,
)


class DistributedOptimizer:
    """Flat, DP-sharded AdamW.

    Parameters
    ----------
    model:
        The local model shard (TP/PP already applied).  All parameters with
        ``requires_grad`` are flattened in ``model.parameters()`` order.
    lr, betas, eps, weight_decay:
        AdamW hyper-parameters.  ``weight_decay`` is applied only to parameters
        with ``ndim > 1`` and last dimension ``!= 1``, matching
        :func:`mini_megatron.optim.adamw.build_optimizer`.
    group:
        Data-parallel process group.  ``None`` means the default group; a
        world size of 1 degenerates to ordinary AdamW, which is useful for
        unit tests.
    """

    def __init__(
        self,
        model: torch.nn.Module,
        *,
        lr: float,
        betas=(0.9, 0.95),
        eps: float = 1e-8,
        weight_decay: float = 0.1,
        group=None,
    ) -> None:
        self.group = group
        # With ``group=None`` the default process group is used.  Its size is
        # the world size; its rank is the global rank (not always 0 when the
        # caller forgot to pass a DP group).
        self.world = comm.get_group_size(group)
        self.rank = comm.get_group_rank(group) if group is not None else comm.get_rank()
        self.lr = float(lr)
        self.beta1, self.beta2 = float(betas[0]), float(betas[1])
        self.eps = float(eps)
        self.weight_decay = float(weight_decay)
        self.tp_size = max(1, get_tensor_model_parallel_world_size())
        self.cp_size = max(1, get_context_parallel_world_size())

        self.params: List[torch.nn.Parameter] = [
            p for p in model.parameters() if p.requires_grad
        ]
        if not self.params:
            raise ValueError("DistributedOptimizer needs at least one parameter")
        self.device = self.params[0].device
        self.numels = [p.numel() for p in self.params]
        self.total_numel = sum(self.numels)
        # Pad so the flat buffer can be split evenly across DP ranks.  The pad
        # is zero in the gradient and zero in the weight-decay mask, so it
        # never changes a real parameter.
        self.padded_numel = (
            (self.total_numel + self.world - 1) // self.world
        ) * self.world
        self.shard_numel = self.padded_numel // self.world
        self.shard_start = self.rank * self.shard_numel
        self.shard_end = self.shard_start + self.shard_numel

        self.master = torch.zeros(
            self.padded_numel, dtype=torch.float32, device=self.device
        )
        self.grad_flat = torch.zeros(
            self.padded_numel, dtype=torch.float32, device=self.device
        )
        self.decay_mask = torch.zeros(
            self.padded_numel, dtype=torch.float32, device=self.device
        )
        # Per-element weight for the global norm.  Parameters replicated across
        # TP (norms, routers, row-parallel biases) are present on every TP rank;
        # counting them ``tp`` times would inflate the clip denominator.
        self.norm_weight = torch.zeros(
            self.padded_numel, dtype=torch.float32, device=self.device
        )
        self.exp_avg = torch.zeros(
            self.shard_numel, dtype=torch.float32, device=self.device
        )
        self.exp_avg_sq = torch.zeros(
            self.shard_numel, dtype=torch.float32, device=self.device
        )
        self.step_count = 0
        self._prepared = False

        # Copy the current weights into the fp32 master buffer and build the
        # per-element decay/norm masks.  The masks are sharded implicitly: only
        # the local slice is used at update time.
        offset = 0
        for p, n in zip(self.params, self.numels):
            self.master[offset : offset + n].copy_(p.detach().reshape(-1).float())
            decay = 0.0 if (p.ndim <= 1 or p.shape[-1] == 1) else 1.0
            self.decay_mask[offset : offset + n].fill_(decay)
            replicate = (
                1.0 / self.tp_size if getattr(p, "_is_replicated", False) else 1.0
            )
            self.norm_weight[offset : offset + n].fill_(replicate)
            offset += n

    # ------------------------------------------------------------------ state
    @property
    def master_shard(self) -> torch.Tensor:
        return self.master[self.shard_start : self.shard_end]

    def zero_grad(self, set_to_none: bool = True) -> None:
        for p in self.params:
            if set_to_none:
                p.grad = None
            elif p.grad is not None:
                p.grad.zero_()
        self._prepared = False

    def set_lr(self, lr: float) -> None:
        self.lr = float(lr)

    # -------------------------------------------------------------- gradients
    def _pack_gradients(self) -> None:
        """Copy per-parameter grads into the flat buffer.

        Missing gradients (data-dependent MoE experts) become zeros.  This is
        the same collective-symmetry rule as ``all_reduce_gradients``: every
        rank must enter reduce-scatter with the same buffer shape.
        """
        self.grad_flat.zero_()
        offset = 0
        for p, n in zip(self.params, self.numels):
            if p.grad is not None:
                self.grad_flat[offset : offset + n].copy_(
                    p.grad.detach().reshape(-1).float()
                )
            offset += n

    def prepare(self) -> None:
        """Reduce gradients across DP ranks into the local shard.

        After this call ``self.grad_flat`` is no longer authoritative; the
        local shard holds the DP-mean gradient for its flat range.
        """
        self._pack_gradients()
        self.grad_shard = torch.empty(
            self.shard_numel, dtype=torch.float32, device=self.device
        )
        comm.reduce_scatter_tensor(
            self.grad_shard, self.grad_flat, op=comm.SUM, group=self.group
        )
        if self.world > 1:
            self.grad_shard.div_(self.world)
        self._prepared = True

    def grad_norm(self) -> torch.Tensor:
        """Global L2 norm of the DP-averaged gradient.

        The flat shard is a local *TP/PP* slice as well as a DP slice, so the
        norm must be reduced over the whole world, not only over the DP group.
        TP-replicated parameters (norms, routers, row-parallel biases) are
        weighted by ``1/tp`` so they are counted once.  CP sums gradients but
        then every CP rank holds the same sum; dividing by ``cp`` avoids
        counting those replicas ``cp`` times.
        """
        if not self._prepared:
            self.prepare()
        weighted = (
            self.grad_shard.detach().pow(2)
            * self.norm_weight[self.shard_start : self.shard_end]
        )
        local = weighted.sum()
        total = comm.all_reduce(local, op=comm.SUM, group=None)
        return (total / self.cp_size).sqrt()

    def clip_grad_norm(self, max_norm: float) -> torch.Tensor:
        norm = self.grad_norm()
        if max_norm and max_norm > 0:
            scale = (max_norm / (norm + 1e-6)).clamp(max=1.0)
            self.grad_shard.mul_(scale)
        return norm

    # ------------------------------------------------------------------ update
    @torch.no_grad()
    def step(self) -> None:
        if not self._prepared:
            self.prepare()
        self.step_count += 1
        t = self.step_count

        g = self.grad_shard
        m = self.exp_avg
        v = self.exp_avg_sq
        # Decoupled weight decay, then the Adam moment update.  The order
        # matches torch.optim.AdamW, which is what the equivalence test uses.
        shard = self.master_shard
        shard.mul_(1.0 - self.lr * self.weight_decay * self.decay_mask[
            self.shard_start : self.shard_end
        ])
        m.lerp_(g, 1.0 - self.beta1)
        v.mul_(self.beta2).addcmul_(g, g, value=1.0 - self.beta2)

        bias_correction1 = 1.0 - self.beta1 ** t
        bias_correction2 = 1.0 - self.beta2 ** t
        denom = (v.sqrt() / math.sqrt(bias_correction2)).add_(self.eps)
        shard.addcdiv_(m, denom, value=-self.lr / bias_correction1)

        # Make the updated weights visible on every DP rank.  Each rank
        # contributes exactly its own shard; concatenation is the inverse of
        # the reduce-scatter split.
        if self.world > 1:
            shards = [
                torch.empty(
                    self.shard_numel, dtype=torch.float32, device=self.device
                )
                for _ in range(self.world)
            ]
            comm.all_gather(shards, shard, group=self.group)
            full = torch.cat(shards)[: self.total_numel]
        else:
            full = self.master[: self.total_numel]
        self._scatter_params(full)
        self._prepared = False

    def _scatter_params(self, flat: torch.Tensor) -> None:
        offset = 0
        for p, n in zip(self.params, self.numels):
            p.data.copy_(flat[offset : offset + n].view_as(p).to(p.dtype))
            offset += n

    # -------------------------------------------------------------- checkpoint
    def state_dict(self) -> Dict:
        return {
            "format": "mini-distributed-optimizer/v1",
            "world": self.world,
            "rank": self.rank,
            "total_numel": self.total_numel,
            "padded_numel": self.padded_numel,
            "step": self.step_count,
            "master_shard": self.master_shard.detach().cpu().clone(),
            "exp_avg": self.exp_avg.detach().cpu().clone(),
            "exp_avg_sq": self.exp_avg_sq.detach().cpu().clone(),
            "lr": self.lr,
        }

    def load_state_dict(self, state: Dict) -> None:
        if state.get("format") != "mini-distributed-optimizer/v1":
            raise ValueError("not a mini distributed-optimizer checkpoint")
        if int(state["world"]) != self.world:
            raise ValueError(
                "distributed-optimizer checkpoint was written with a different "
                f"DP size ({state['world']} != {self.world})"
            )
        if int(state["padded_numel"]) != self.padded_numel:
            raise ValueError("parameter layout changed; cannot load in place")
        self.step_count = int(state["step"])
        device = self.master.device
        self.exp_avg.copy_(state["exp_avg"].float().to(device))
        self.exp_avg_sq.copy_(state["exp_avg_sq"].float().to(device))

        # Reconstruct the full master buffer from the saved shards.  Loading
        # only the local shard would leave other ranks' parameter slices stale.
        local = state["master_shard"].float().to(device)
        if self.world > 1:
            shards = [
                torch.empty(
                    self.shard_numel, dtype=torch.float32, device=device
                )
                for _ in range(self.world)
            ]
            comm.all_gather(shards, local, group=self.group)
            full = torch.cat(shards)[: self.total_numel]
        else:
            full = local[: self.total_numel]
        self.master[: self.total_numel].copy_(full)
        self._scatter_params(full)
        self._prepared = False

    # -------------------------------------------------------------- diagnostics
    def memory_report(self) -> Dict[str, float]:
        """Per-rank bytes for the four state components, in MiB.

        ``weights`` and ``gradients`` are full replicas in ZeRO-1; the
        optimizer moments are sharded.  The fp32 master is also sharded in this
        implementation, which is why the shard accounts for 12 bytes/element
        rather than 8.
        """
        mib = 1024.0 * 1024.0
        weights = sum(
            p.numel() * p.element_size() for p in self.params
        ) / mib
        grads = sum(
            (p.grad.numel() * p.grad.element_size() if p.grad is not None else 0)
            for p in self.params
        ) / mib
        return {
            "weights_mib": weights,
            "gradients_mib": grads,
            "master_mib": self.shard_numel * 4 / mib,
            "exp_avg_mib": self.shard_numel * 4 / mib,
            "exp_avg_sq_mib": self.shard_numel * 4 / mib,
        }
