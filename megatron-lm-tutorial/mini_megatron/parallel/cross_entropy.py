"""Vocabulary-parallel cross entropy.

Each TP rank owns a contiguous vocabulary shard.  The log-sum-exp is reduced
over shards, while the target logit is non-zero on exactly one shard.  The
context-parallel reduction makes the loss a true mean over all tokens, not a
mean of per-rank means.
"""
from __future__ import annotations

from typing import Optional

import torch
from . import comm
from .state import (
    _group_size,
    _group_rank,
    get_context_parallel_group,
    get_tensor_model_parallel_group,
)


class _VocabParallelCrossEntropy(torch.autograd.Function):
    @staticmethod
    def forward(
        ctx,
        logits: torch.Tensor,
        labels: torch.Tensor,
        tp_group,
        cp_group,
        ignore_index: int,
        z_loss_coeff: float,
    ) -> torch.Tensor:
        logits = logits.contiguous()
        labels = labels.contiguous()
        # Log-sum-exp is numerically sensitive; do it in fp32 even when the
        # model runs under bf16 autocast.  The returned gradient is cast back
        # to the logits dtype so autograd sees a matching tensor.
        work = logits.float()
        tp_world = _group_size(tp_group)
        cp_world = _group_size(cp_group)
        rank = _group_rank(tp_group) if tp_world > 1 else 0
        vocab_local = logits.size(-1)
        vocab_start = rank * vocab_local

        valid = labels != ignore_index
        # max over the local vocabulary shard
        max_local = work.max(dim=-1).values
        max_global = max_local
        if tp_world > 1:
            max_global = comm.all_reduce(max_local, op=comm.MAX, group=tp_group)

        shifted = work - max_global.unsqueeze(-1)
        exp_local = torch.exp(shifted)
        sum_local = exp_local.sum(dim=-1)
        sum_global = sum_local
        if tp_world > 1:
            sum_global = comm.all_reduce(sum_local, op=comm.SUM, group=tp_group)

        logsumexp = max_global + torch.log(sum_global.clamp_min(1e-30))

        target_mask = valid & (labels >= vocab_start) & (labels < vocab_start + vocab_local)
        local_target = (labels - vocab_start).clamp(0, vocab_local - 1)
        target_logit = work.gather(1, local_target.unsqueeze(1)).squeeze(1)
        target_logit = target_logit.masked_fill(~target_mask, 0.0)
        if tp_world > 1:
            target_logit = comm.all_reduce(target_logit, op=comm.SUM, group=tp_group)

        token_loss = (logsumexp - target_logit) * valid.to(work.dtype)
        if z_loss_coeff:
            token_loss = token_loss + z_loss_coeff * (logsumexp**2) * valid.to(
                work.dtype
            )

        local_count = valid.sum().to(torch.float64)
        local_sum = token_loss.sum().to(torch.float64)
        global_count = local_count
        global_sum = local_sum
        if cp_world > 1:
            stats = torch.stack((local_sum, local_count))
            stats = comm.all_reduce(stats, op=comm.SUM, group=cp_group)
            global_sum, global_count = stats[0], stats[1]

        if global_count.item() == 0:
            loss = (work.sum() * 0.0).to(logits.dtype)
            ctx.global_count = 0.0
        else:
            loss = (global_sum / global_count).to(logits.dtype)
            ctx.global_count = float(global_count.item())

        ctx.save_for_backward(work, labels, max_global, sum_global)
        ctx.logits_dtype = logits.dtype
        ctx.tp_group = tp_group
        ctx.ignore_index = ignore_index
        ctx.z_loss_coeff = z_loss_coeff
        ctx.vocab_local = vocab_local
        ctx.vocab_start = vocab_start
        return loss

    @staticmethod
    def backward(ctx, grad_output: torch.Tensor):
        logits, labels, max_global, sum_global = ctx.saved_tensors
        if ctx.global_count == 0:
            return torch.zeros_like(logits, dtype=ctx.logits_dtype), None, None, None, None, None

        # softmax over the *global* vocabulary, computed locally in fp32.
        probs = torch.exp(logits - max_global.unsqueeze(-1)) / sum_global.unsqueeze(-1)
        valid = (labels != ctx.ignore_index).to(probs.dtype)
        grad = probs * valid.unsqueeze(-1)

        target_mask = (
            (labels != ctx.ignore_index)
            & (labels >= ctx.vocab_start)
            & (labels < ctx.vocab_start + ctx.vocab_local)
        )
        local_target = (labels - ctx.vocab_start).clamp(0, ctx.vocab_local - 1)
        grad.scatter_add_(
            1,
            local_target.unsqueeze(1),
            -target_mask.to(probs.dtype).unsqueeze(1),
        )

        if ctx.z_loss_coeff:
            logsumexp = max_global + torch.log(sum_global.clamp_min(1e-30))
            grad = grad + (
                2.0
                * ctx.z_loss_coeff
                * logsumexp.unsqueeze(-1)
                * probs
                * valid.unsqueeze(-1)
            )

        grad = grad * (grad_output / ctx.global_count)
        return grad.to(ctx.logits_dtype), None, None, None, None, None


def vocab_parallel_cross_entropy(
    logits: torch.Tensor,
    labels: torch.Tensor,
    *,
    ignore_index: int = -100,
    z_loss_coeff: float = 0.0,
    tp_group=None,
    cp_group=None,
) -> torch.Tensor:
    """Return the mean token cross entropy over the global vocabulary.

    ``logits`` may be ``[..., vocab_local]`` and ``labels`` ``[...]``; both are
    flattened internally.  The result is a scalar tensor on the logits device.
    """
    original_shape = labels.shape
    logits_2d = logits.reshape(-1, logits.size(-1))
    labels_1d = labels.reshape(-1)
    if logits_2d.size(0) != labels_1d.size(0):
        raise ValueError(
            f"logits/labels token mismatch: {logits_2d.size(0)} vs {labels_1d.size(0)}"
        )
    tp_group = tp_group or get_tensor_model_parallel_group()
    cp_group = cp_group or get_context_parallel_group()
    loss = _VocabParallelCrossEntropy.apply(
        logits_2d, labels_1d, tp_group, cp_group, ignore_index, z_loss_coeff
    )
    # The scalar is intentionally shared by all ranks; no further reduction.
    _ = original_shape
    return loss
