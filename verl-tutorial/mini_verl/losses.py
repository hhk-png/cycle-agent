"""Loss functions shared by engines, workers and trainers."""

from __future__ import annotations

from typing import Any

import numpy as np

from .algo.core_algos import (
    agg_loss,
    compute_entropy_loss,
    compute_policy_loss,
    compute_policy_loss_cispo,
    compute_policy_loss_gspo,
    compute_value_loss,
    masked_mean,
)
from .autograd import Tensor, gather_last_dim, log_softmax, no_grad, where
from .nn import entropy_from_logits, log_probs_from_logits

__all__ = [
    "logprob_metric_fn",
    "value_metric_fn",
    "actor_loss_fn",
    "critic_loss_fn",
    "sft_loss_fn",
    "dpo_loss_fn",
    "distillation_loss_fn",
    "kl_penalty_tensor",
]


def logprob_metric_fn(model, batch: dict[str, Any]):
    logits = model(batch["input_ids"])
    log_probs = log_probs_from_logits(
        logits, batch["input_ids"], batch["response_mask"], batch.get("allowed_token_ids")
    )
    return log_probs, {}


def value_metric_fn(model, batch: dict[str, Any]):
    values = model(batch["input_ids"]) * batch["response_mask"]
    return values, {}


def kl_penalty_tensor(log_prob: Tensor, ref_log_prob: Tensor, kind: str = "low_var_kl") -> Tensor:
    diff = log_prob - ref_log_prob
    if kind in {"kl", "k1"}:
        return diff
    if kind == "abs":
        # Tensor has no comparison dunders; use the functional select instead.
        return where(diff.data > 0, diff, -diff)
    if kind in {"mse", "k2"}:
        return (diff * diff) * 0.5
    if kind in {"low_var_kl", "k3"}:
        kl = (ref_log_prob - log_prob).clip(-20.0, 20.0)
        return (kl.exp() - kl - 1.0).clip(-10.0, 10.0)
    raise ValueError(f"unknown kl loss type {kind!r}")


def _global_info(batch: dict[str, Any], fallback_tokens: float) -> dict[str, float]:
    return {
        "dp_size": float(batch.get("_dp_size", 1)),
        "batch_num_tokens": float(batch.get("_global_num_tokens", fallback_tokens)),
        "global_batch_size": float(batch.get("_global_batch_size", batch["response_mask"].shape[0])),
    }


def actor_loss_fn(model, batch: dict[str, Any]):
    input_ids = batch["input_ids"]
    mask = batch["response_mask"]
    logits = model(input_ids)
    log_probs = log_probs_from_logits(logits, input_ids, mask, batch.get("allowed_token_ids"))
    old_log_probs = Tensor(batch["old_log_probs"])
    advantages = batch["advantages"]
    info = _global_info(batch, float(mask.sum()))

    # GSPO's ratio is constant per sequence, so the matching objective is
    # seq-mean-token-mean.  Letting a global token-mean override it would
    # weight long sequences more heavily and silently change the algorithm.
    loss_agg_mode = (
        "seq-mean-token-mean"
        if batch.get("use_gspo", False)
        else batch.get("loss_agg_mode", "token-mean")
    )

    if batch.get("use_cispo", False):
        pg_loss, metrics = compute_policy_loss_cispo(
            old_log_probs,
            log_probs,
            advantages,
            mask,
            clip_ratio=batch.get("clip_ratio", 0.2),
            clip_ratio_low=batch.get("clip_ratio_low"),
            clip_ratio_high=batch.get("clip_ratio_high"),
            loss_agg_mode=loss_agg_mode,
            rollout_is_weights=batch.get("rollout_is_weights"),
            **info,
        )
    elif batch.get("use_gspo", False):
        pg_loss, metrics = compute_policy_loss_gspo(
            old_log_probs,
            log_probs,
            advantages,
            mask,
            clip_ratio=batch.get("clip_ratio", 0.2),
            clip_ratio_low=batch.get("clip_ratio_low"),
            clip_ratio_high=batch.get("clip_ratio_high"),
            loss_agg_mode=loss_agg_mode,
            rollout_is_weights=batch.get("rollout_is_weights"),
            **info,
        )
    else:
        pg_loss, metrics = compute_policy_loss(
            old_log_probs,
            log_probs,
            advantages,
            mask,
            clip_ratio=batch.get("clip_ratio", 0.2),
            clip_ratio_low=batch.get("clip_ratio_low"),
            clip_ratio_high=batch.get("clip_ratio_high"),
            clip_ratio_c=batch.get("clip_ratio_c", 3.0),
            loss_agg_mode=loss_agg_mode,
            rollout_is_weights=batch.get("rollout_is_weights"),
            **info,
        )

    loss = pg_loss
    metrics["actor/pg_loss"] = float(pg_loss.item())

    if batch.get("use_kl_loss", False):
        ref = Tensor(batch["ref_log_prob"])
        kl = kl_penalty_tensor(log_probs, ref, batch.get("kl_loss_type", "low_var_kl"))
        kl_loss = agg_loss(kl * mask, mask, loss_agg_mode, **info)
        loss = loss + kl_loss * float(batch.get("kl_loss_coef", 0.001))
        metrics["actor/kl_loss"] = float(kl_loss.item())
        metrics["actor/kl"] = masked_mean(np.asarray(kl.data), mask)

    if batch.get("calculate_entropy", True):
        from .nn import apply_vocab_mask

        # Logits at position t predict token t+1.  Entropy must therefore be
        # shifted together with the mask: the old code multiplied logits[t] by
        # mask[t], which credits the last prompt token and the last padding
        # position instead of the actual response tokens.
        shifted_logits = apply_vocab_mask(logits, batch.get("allowed_token_ids"))[:, :-1]
        shifted_mask = mask[:, 1:]
        entropy = entropy_from_logits(shifted_logits, shifted_mask)
        entropy_loss = compute_entropy_loss(entropy, shifted_mask, loss_agg_mode, **info)
        # ``entropy_loss`` is -mean(H).  Adding it with a positive coefficient
        # lowers the loss when entropy is high, i.e. it is the entropy *bonus*.
        # Subtracting it would penalize exploration.
        loss = loss + entropy_loss * float(batch.get("entropy_coeff", 0.0))
        metrics["actor/entropy"] = masked_mean(np.asarray(entropy.data), shifted_mask)

    metrics["actor/loss"] = float(loss.item())
    return loss, metrics


def critic_loss_fn(model, batch: dict[str, Any]):
    mask = batch["response_mask"]
    values_pred = model(batch["input_ids"]) * mask
    info = _global_info(batch, float(mask.sum()))
    loss, clipfrac = compute_value_loss(
        values_pred,
        batch["returns"],
        batch["values"],
        mask,
        cliprange_value=batch.get("cliprange_value", 0.5),
        loss_agg_mode=batch.get("loss_agg_mode", "token-mean"),
        **info,
    )
    metrics = {"critic/vf_loss": float(loss.item()), "critic/vf_clipfrac": clipfrac}
    return loss, metrics


def sft_loss_fn(model, batch: dict[str, Any]):
    input_ids = batch["input_ids"]
    mask = batch["response_mask"]
    logits = model(input_ids)
    shifted = logits[:, :-1, :]
    log_probs = log_softmax(shifted, axis=-1)
    gathered = gather_last_dim(log_probs, input_ids[:, 1:])
    # Align with position t (the target token at t).
    from .autograd import concat

    zero = Tensor(np.zeros((input_ids.shape[0], 1), dtype=np.float32))
    aligned = concat([zero, gathered], axis=1)
    info = _global_info(batch, float(mask.sum()))
    # Normalize by the *global* token count and let the DP all-reduce average
    # the ranks; local-token normalization would weight short ranks too much.
    loss = agg_loss(-aligned, mask, "token-mean", **info)
    tokens = info["batch_num_tokens"]
    metrics = {"sft/loss": float(loss.item()), "sft/tokens": tokens}
    return loss, metrics


def _sequence_logprob(
    model,
    prompt_ids: np.ndarray,
    response_ids: np.ndarray,
    response_mask: np.ndarray,
    prompt_lengths: np.ndarray | None = None,
) -> Tensor:
    """Average response-token log-probability.

    ``prompt_ids`` is usually a rectangular padded array.  Concatenating the
    whole array would put pad tokens between the real prompt and the response,
    changing the context the model sees.  When ``prompt_lengths`` is available
    we compact each row to ``prompt[:length] + response`` and pad only at the
    end.
    """

    prompt_ids = np.asarray(prompt_ids, dtype=np.int64)
    response_ids = np.asarray(response_ids, dtype=np.int64)
    response_mask = np.asarray(response_mask, dtype=np.float32)
    batch = prompt_ids.shape[0]
    if prompt_lengths is None:
        lengths = np.full(batch, prompt_ids.shape[1], dtype=np.int64)
    else:
        lengths = np.asarray(prompt_lengths, dtype=np.int64)
    seq_lengths = np.maximum(lengths + response_mask.sum(axis=-1).astype(np.int64), 1)
    max_len = int(seq_lengths.max())
    input_ids = np.zeros((batch, max_len), dtype=np.int64)
    full_mask = np.zeros((batch, max_len), dtype=np.float32)
    for row in range(batch):
        prompt_len = int(lengths[row])
        # Compact by mask, not by ``[:mask.sum()]``: a mask with holes (e.g.
        # tool observations) must not silently drop every token after the
        # first hole.
        selected = response_ids[row][response_mask[row] > 0]
        prompt = prompt_ids[row, :prompt_len]
        sequence = np.concatenate([prompt, selected])
        input_ids[row, : len(sequence)] = sequence
        full_mask[row, prompt_len : prompt_len + len(selected)] = 1.0
    logits = model(input_ids)
    log_probs = log_probs_from_logits(logits, input_ids, full_mask)
    # DPO uses the *sum* of response log-probs; length-normalizing here would
    # make the effective beta depend on response length.
    return (log_probs * full_mask).sum(axis=-1)


def dpo_loss_fn(model, batch: dict[str, Any], ref_model=None, beta: float = 0.1):
    prompt_lengths = batch.get("prompt_lengths")
    chosen = _sequence_logprob(
        model, batch["prompt_ids"], batch["chosen_ids"], batch["chosen_mask"], prompt_lengths
    )
    rejected = _sequence_logprob(
        model, batch["prompt_ids"], batch["rejected_ids"], batch["rejected_mask"], prompt_lengths
    )
    if ref_model is None:
        ref_chosen = Tensor(np.zeros_like(chosen.data))
        ref_rejected = Tensor(np.zeros_like(rejected.data))
    else:
        # The reference model is a fixed anchor: do not build a graph through
        # it, and do not let its parameters receive gradients.
        with no_grad():
            ref_chosen = _sequence_logprob(
                ref_model, batch["prompt_ids"], batch["chosen_ids"], batch["chosen_mask"], prompt_lengths
            )
            ref_rejected = _sequence_logprob(
                ref_model, batch["prompt_ids"], batch["rejected_ids"], batch["rejected_mask"], prompt_lengths
            )
    logits = beta * ((chosen - rejected) - (ref_chosen - ref_rejected))
    # -log sigmoid(x) = softplus(-x)
    per_pair = _softplus(-logits)
    if "loss_mask" in batch:
        mask = np.asarray(batch["loss_mask"], dtype=np.float32)
        # DP all-reduce averages gradients across ranks.  Normalize by the
        # *global* pair count and multiply by dp_size so the averaged gradient
        # equals the single-rank global mean even when N % world_size != 0.
        global_pairs = float(batch.get("_global_num_pairs", 0.0) or 0.0)
        if global_pairs <= 0:
            global_pairs = float(mask.sum())
        dp_size = float(batch.get("_dp_size", 1.0) or 1.0)
        loss = (per_pair * mask).sum() / max(global_pairs, 1.0) * dp_size
    else:
        loss = per_pair.mean()
    metrics = {
        "dpo/loss": float(loss.item()),
        "dpo/chosen_logp": float(chosen.data.mean()),
        "dpo/rejected_logp": float(rejected.data.mean()),
        "dpo/margin": float((chosen.data - rejected.data).mean()),
    }
    return loss, metrics


def _softplus(x: Tensor) -> Tensor:
    # Stable log(1+exp(x)) = max(x,0) + log(1+exp(-|x|)).  Do not clip the
    # input: Tensor.clip has zero gradient outside the interval, which would
    # silently stop correcting strongly-wrong DPO pairs.
    from .autograd import maximum

    abs_x = maximum(x, -x)
    return maximum(x, 0.0) + ((-abs_x).exp() + 1.0).log()


def distillation_loss_fn(
    student_logits: Tensor,
    teacher_logits: np.ndarray,
    response_mask: np.ndarray,
    temperature: float = 1.0,
    dp_size: int = 1,
    batch_num_tokens: float | None = None,
    global_batch_size: float | None = None,
    loss_agg_mode: str = "token-mean",
    loss_scale_factor: float | None = None,
):
    """Forward KL(teacher || student) on response tokens.

    Uses the same global-token aggregation as SFT/actor: a local
    ``/mask.sum()`` would give small DP chunks a larger per-token weight.
    """
    student_log_probs = log_softmax(student_logits / temperature, axis=-1)
    teacher = np.asarray(teacher_logits, dtype=np.float32) / temperature
    teacher = teacher - teacher.max(axis=-1, keepdims=True)
    teacher_probs = np.exp(teacher)
    teacher_probs /= teacher_probs.sum(axis=-1, keepdims=True)
    per_token = -(student_log_probs * teacher_probs).sum(axis=-1)
    loss = agg_loss(
        per_token,
        np.asarray(response_mask, dtype=np.float32),
        loss_agg_mode,
        dp_size=dp_size,
        batch_num_tokens=batch_num_tokens,
        global_batch_size=global_batch_size,
        loss_scale_factor=loss_scale_factor,
    )
    return loss, {"distillation/kl": float(loss.item())}
