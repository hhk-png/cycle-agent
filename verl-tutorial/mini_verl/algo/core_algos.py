"""Advantage estimators, KL estimators and PPO-family losses.

The NumPy functions mirror ``verl/trainer/ppo/core_algos.py``.  The loss
functions intentionally operate on MiniVerl ``Tensor`` objects so that the
actor/critic update is genuinely differentiable.
"""

from __future__ import annotations

from collections import defaultdict

import numpy as np

from ..autograd import Tensor, clip, maximum, minimum, where

__all__ = [
    "masked_mean",
    "masked_sum",
    "masked_whiten",
    "compute_gae_advantage_return",
    "compute_grpo_outcome_advantage",
    "compute_rloo_outcome_advantage",
    "compute_reinforce_plus_plus_outcome_advantage",
    "compute_remax_outcome_advantage",
    "compute_advantage",
    "filter_groups_for_dapo",
    "kl_penalty",
    "compute_rewards",
    "agg_loss",
    "compute_policy_loss",
    "compute_policy_loss_cispo",
    "compute_policy_loss_gspo",
    "compute_value_loss",
    "compute_entropy_loss",
    "FixedKLController",
    "AdaptiveKLController",
    "get_kl_controller",
]


def masked_sum(array: np.ndarray, mask: np.ndarray) -> float:
    return float(np.sum(np.asarray(array) * np.asarray(mask)))


def masked_mean(array: np.ndarray, mask: np.ndarray, eps: float = 1e-8) -> float:
    mask = np.asarray(mask, dtype=np.float32)
    return float(np.sum(np.asarray(array) * mask) / max(mask.sum(), eps))


def masked_whiten(array: np.ndarray, mask: np.ndarray, eps: float = 1e-8) -> np.ndarray:
    values = np.asarray(array, dtype=np.float32)
    mask = np.asarray(mask, dtype=np.float32)
    mean = np.sum(values * mask) / max(mask.sum(), eps)
    variance = np.sum((values - mean) ** 2 * mask) / max(mask.sum(), eps)
    return (values - mean) / np.sqrt(variance + eps)


# ----------------------------------------------------------------------
# advantage estimators
# ----------------------------------------------------------------------


def compute_gae_advantage_return(
    token_level_rewards: np.ndarray,
    values: np.ndarray,
    response_mask: np.ndarray,
    gamma: float = 1.0,
    lam: float = 1.0,
    truncated: np.ndarray | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    """Generalized Advantage Estimation, per sequence.

    Padding after the response is excluded by walking only up to the last
    valid response token; otherwise the value of a pad position leaks into the
    bootstrap term.
    """
    rewards = np.asarray(token_level_rewards, dtype=np.float32)
    values = np.asarray(values, dtype=np.float32) * np.asarray(response_mask, dtype=np.float32)
    mask = np.asarray(response_mask, dtype=np.float32)
    advantages = np.zeros_like(rewards)
    for row in range(rewards.shape[0]):
        valid = np.flatnonzero(mask[row] > 0)
        if valid.size == 0:
            continue
        last = int(valid[-1])
        last_gae = 0.0
        # Mirrors verl's ``compute_gae_advantage_return``: at a mask=0 position
        # the TD error and the accumulated advantage are discarded, and both
        # ``next_value`` and ``last_gae`` keep their previous values.  They are
        # frozen, not decayed by gamma/lambda.  This is what lets a reward on a
        # later assistant token propagate back across a tool observation.
        next_value = values[row, last] if (truncated is not None and bool(truncated[row])) else 0.0
        for t in range(last, -1, -1):
            delta = rewards[row, t] + gamma * next_value - values[row, t]
            last_gae_ = delta + gamma * lam * last_gae
            if mask[row, t] > 0:
                last_gae = last_gae_
                advantages[row, t] = last_gae
                next_value = values[row, t]
            # mask=0: leave last_gae and next_value untouched.
    returns = advantages + values
    returns *= mask
    return advantages.astype(np.float32), returns.astype(np.float32)


def _group_scores(token_level_rewards: np.ndarray, index: np.ndarray):
    scores = np.asarray(token_level_rewards, dtype=np.float32).sum(axis=-1)
    groups: dict[object, list[float]] = defaultdict(list)
    for score, key in zip(scores, index):
        groups[key].append(float(score))
    means = {key: float(np.mean(values)) for key, values in groups.items()}
    # torch.std defaults to the unbiased (n-1) estimator; matching it matters
    # when a tutorial reader compares MiniVerl numbers with verl.
    stds = {key: (float(np.std(values, ddof=1)) if len(values) > 1 else 1.0) for key, values in groups.items()}
    return scores, groups, means, stds


def compute_grpo_outcome_advantage(
    token_level_rewards: np.ndarray,
    response_mask: np.ndarray,
    index: np.ndarray,
    epsilon: float = 1e-6,
    norm_adv_by_std_in_grpo: bool = True,
) -> tuple[np.ndarray, np.ndarray]:
    scores, groups, means, stds = _group_scores(token_level_rewards, index)
    advantages = np.zeros_like(scores, dtype=np.float32)
    for i, key in enumerate(index):
        if len(groups[key]) == 1:
            mean, std = 0.0, 1.0
        else:
            mean, std = means[key], stds[key]
        if norm_adv_by_std_in_grpo:
            advantages[i] = (scores[i] - mean) / (std + epsilon)
        else:
            advantages[i] = scores[i] - mean
    token_adv = advantages[:, None] * np.asarray(response_mask, dtype=np.float32)
    return token_adv, token_adv.copy()


def compute_rloo_outcome_advantage(
    token_level_rewards: np.ndarray,
    response_mask: np.ndarray,
    index: np.ndarray,
    epsilon: float = 1e-6,
) -> tuple[np.ndarray, np.ndarray]:
    scores, groups, means, _ = _group_scores(token_level_rewards, index)
    advantages = np.zeros_like(scores, dtype=np.float32)
    for i, key in enumerate(index):
        n = len(groups[key])
        if n > 1:
            advantages[i] = scores[i] * n / (n - 1) - means[key] * n / (n - 1)
    token_adv = advantages[:, None] * np.asarray(response_mask, dtype=np.float32)
    return token_adv, token_adv.copy()


def compute_reinforce_plus_plus_outcome_advantage(
    token_level_rewards: np.ndarray,
    response_mask: np.ndarray,
    gamma: float = 1.0,
) -> tuple[np.ndarray, np.ndarray]:
    rewards = np.asarray(token_level_rewards, dtype=np.float32)
    mask = np.asarray(response_mask, dtype=np.float32)
    returns = np.zeros_like(rewards)
    running = np.zeros(rewards.shape[0], dtype=np.float32)
    for t in reversed(range(rewards.shape[1])):
        # Match verl's REINFORCE++: at mask=0 the new return is discarded and
        # ``running`` is frozen (not discounted).  A reward on a later assistant
        # token therefore propagates back across a tool observation unchanged.
        new_running = rewards[:, t] + gamma * running
        returns[:, t] = new_running * mask[:, t]
        running = new_running * mask[:, t] + running * (1.0 - mask[:, t])
    advantages = masked_whiten(returns, mask) * mask
    return advantages.astype(np.float32), returns.astype(np.float32)


def compute_remax_outcome_advantage(
    token_level_rewards: np.ndarray,
    reward_baselines: np.ndarray,
    response_mask: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    rewards = np.asarray(token_level_rewards, dtype=np.float32)
    mask = np.asarray(response_mask, dtype=np.float32)
    returns = np.flip(np.cumsum(np.flip(rewards * mask, axis=-1), axis=-1), axis=-1)
    advantages = returns - np.asarray(reward_baselines, dtype=np.float32)[:, None] * mask
    return advantages.astype(np.float32), returns.astype(np.float32)


def filter_groups_for_dapo(
    scores: np.ndarray,
    index: np.ndarray,
    metric: str = "reward",
) -> tuple[np.ndarray, dict[str, float]]:
    """DAPO dynamic sampling: keep prompt groups whose reward is not constant.

    A group where every sample receives the same reward has zero GRPO
    advantage and contributes no gradient.  DAPO removes it and generates more
    prompts instead of wasting the update.
    """
    groups: dict[object, list[float]] = defaultdict(list)
    for score, key in zip(scores, index):
        groups[key].append(float(score))
    keep_keys = {key for key, values in groups.items() if len(set(values)) > 1}
    keep = np.asarray([i for i, key in enumerate(index) if key in keep_keys], dtype=np.int64)
    stats = {
        "dapo/filtered_groups": float(len(groups) - len(keep_keys)),
        "dapo/kept_groups": float(len(keep_keys)),
        "dapo/filtered_ratio": float(1.0 - len(keep_keys) / max(len(groups), 1)),
    }
    return keep, stats


def compute_advantage(
    adv_estimator: str,
    token_level_rewards: np.ndarray,
    response_mask: np.ndarray,
    values: np.ndarray | None = None,
    index: np.ndarray | None = None,
    gamma: float = 1.0,
    lam: float = 1.0,
    norm_adv_by_std_in_grpo: bool = True,
    reward_baselines: np.ndarray | None = None,
    truncated: np.ndarray | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    if adv_estimator == "gae":
        if values is None:
            raise ValueError("gae requires critic values")
        return compute_gae_advantage_return(
            token_level_rewards, values, response_mask, gamma, lam, truncated
        )
    if adv_estimator in {"grpo", "dapo", "drgrpo"}:
        if index is None:
            raise ValueError(f"{adv_estimator} requires a prompt-group index")
        # DAPO uses the same group-relative estimator; its differences live in
        # clip ranges, loss aggregation and dynamic sampling.
        norm = adv_estimator != "drgrpo" and norm_adv_by_std_in_grpo
        return compute_grpo_outcome_advantage(
            token_level_rewards, response_mask, index, norm_adv_by_std_in_grpo=norm
        )
    if adv_estimator == "rloo":
        if index is None:
            raise ValueError("rloo requires a prompt-group index")
        return compute_rloo_outcome_advantage(token_level_rewards, response_mask, index)
    if adv_estimator == "reinforce_plus_plus":
        return compute_reinforce_plus_plus_outcome_advantage(token_level_rewards, response_mask, gamma)
    if adv_estimator == "remax":
        if reward_baselines is None:
            raise ValueError("remax requires reward baselines")
        return compute_remax_outcome_advantage(token_level_rewards, reward_baselines, response_mask)
    if adv_estimator == "gspo":
        # GSPO is a loss, not an advantage estimator; it still uses group-relative
        # advantages on top of sequence-level ratios.
        if index is None:
            raise ValueError("gspo requires a prompt-group index")
        return compute_grpo_outcome_advantage(token_level_rewards, response_mask, index)
    raise ValueError(f"unknown advantage estimator {adv_estimator!r}")


# ----------------------------------------------------------------------
# KL
# ----------------------------------------------------------------------


def kl_penalty(logprob: np.ndarray, ref_logprob: np.ndarray, kind: str = "kl") -> np.ndarray:
    logprob = np.asarray(logprob, dtype=np.float32)
    ref_logprob = np.asarray(ref_logprob, dtype=np.float32)
    if kind in {"kl", "k1"}:
        return logprob - ref_logprob
    if kind == "abs":
        return np.abs(logprob - ref_logprob)
    if kind in {"mse", "k2"}:
        return 0.5 * (logprob - ref_logprob) ** 2
    if kind in {"low_var_kl", "k3"}:
        kl = np.clip(ref_logprob - logprob, -20.0, 20.0)
        return np.clip(np.exp(kl) - kl - 1.0, -10.0, 10.0)
    if kind == "full":
        raise NotImplementedError("full KL needs vocabulary logits, not scalar log-probs")
    raise ValueError(f"unknown kl penalty {kind!r}")


def compute_rewards(
    token_level_scores: np.ndarray,
    old_log_prob: np.ndarray,
    ref_log_prob: np.ndarray,
    kl_ratio: float,
) -> np.ndarray:
    kl = kl_penalty(old_log_prob, ref_log_prob, "kl")
    return np.asarray(token_level_scores, dtype=np.float32) - kl_ratio * kl


# ----------------------------------------------------------------------
# aggregation and losses
# ----------------------------------------------------------------------


def _is_tensor(value) -> bool:
    return isinstance(value, Tensor)


def _masked_sum_t(loss_mat, mask):
    if _is_tensor(loss_mat):
        return (loss_mat * mask).sum()
    return float(np.sum(np.asarray(loss_mat) * np.asarray(mask)))


def _masked_seq_sum_t(loss_mat, mask):
    if _is_tensor(loss_mat):
        return (loss_mat * mask).sum(axis=-1)
    return np.sum(np.asarray(loss_mat) * np.asarray(mask), axis=-1)


def agg_loss(
    loss_mat,
    loss_mask,
    loss_agg_mode: str = "token-mean",
    dp_size: int = 1,
    batch_num_tokens: float | None = None,
    global_batch_size: float | None = None,
    loss_scale_factor: float | None = None,
):
    mask = np.asarray(loss_mask, dtype=np.float32)
    if loss_agg_mode == "token-mean":
        if batch_num_tokens is None:
            batch_num_tokens = float(mask.sum())
        return _masked_sum_t(loss_mat, mask) / max(float(batch_num_tokens), 1e-8) * dp_size
    if loss_agg_mode == "token-sum":
        return _masked_sum_t(loss_mat, mask) * dp_size
    if loss_agg_mode in {"seq-mean-token-sum", "seq-mean-token-sum-norm"}:
        seq_losses = _masked_seq_sum_t(loss_mat, mask)
        seq_mask = (mask.sum(axis=-1) > 0).astype(np.float32)
        if global_batch_size is None:
            global_batch_size = float(seq_mask.sum())
        if _is_tensor(seq_losses):
            loss = (seq_losses * seq_mask).sum() / max(float(global_batch_size), 1e-8) * dp_size
        else:
            loss = float(np.sum(seq_losses * seq_mask) / max(float(global_batch_size), 1e-8) * dp_size)
        if loss_agg_mode == "seq-mean-token-sum-norm":
            horizon = loss_scale_factor or mask.shape[-1]
            loss = loss / horizon
        return loss
    if loss_agg_mode == "seq-mean-token-mean":
        lengths = mask.sum(axis=-1)
        seq_losses = _masked_seq_sum_t(loss_mat, mask) / np.maximum(lengths, 1e-8)
        seq_mask = (lengths > 0).astype(np.float32)
        if global_batch_size is None:
            global_batch_size = float(seq_mask.sum())
        if _is_tensor(seq_losses):
            return (seq_losses * seq_mask).sum() / max(float(global_batch_size), 1e-8) * dp_size
        return float(np.sum(seq_losses * seq_mask) / max(float(global_batch_size), 1e-8) * dp_size)
    raise ValueError(f"invalid loss_agg_mode {loss_agg_mode!r}")


def compute_policy_loss(
    old_log_prob: Tensor,
    log_prob: Tensor,
    advantages: np.ndarray,
    response_mask: np.ndarray,
    clip_ratio: float = 0.2,
    clip_ratio_low: float | None = None,
    clip_ratio_high: float | None = None,
    clip_ratio_c: float = 3.0,
    loss_agg_mode: str = "token-mean",
    dp_size: int = 1,
    batch_num_tokens: float | None = None,
    global_batch_size: float | None = None,
    loss_scale_factor: float | None = None,
    rollout_is_weights: np.ndarray | None = None,
) -> tuple[Tensor, dict[str, float]]:
    low = clip_ratio if clip_ratio_low is None else clip_ratio_low
    high = clip_ratio if clip_ratio_high is None else clip_ratio_high
    advantages = np.asarray(advantages, dtype=np.float32)
    mask = np.asarray(response_mask, dtype=np.float32)

    negative_approx_kl = (log_prob - old_log_prob).clip(-20.0, 20.0)
    ratio = negative_approx_kl.exp()
    ppo_kl = masked_mean(-np.asarray(negative_approx_kl.data), mask)

    pg_losses1 = -ratio * advantages
    pg_losses2 = -clip(ratio, 1.0 - low, 1.0 + high) * advantages
    clip_pg_losses1 = maximum(pg_losses1, pg_losses2)
    clip_pg_losses2 = minimum(-advantages * clip_ratio_c, clip_pg_losses1)
    pg_losses = where(advantages < 0, clip_pg_losses2, clip_pg_losses1)
    if rollout_is_weights is not None:
        pg_losses = pg_losses * np.asarray(rollout_is_weights, dtype=np.float32)

    pg_loss = agg_loss(
        pg_losses,
        mask,
        loss_agg_mode,
        dp_size=dp_size,
        batch_num_tokens=batch_num_tokens,
        global_batch_size=global_batch_size,
        loss_scale_factor=loss_scale_factor,
    )
    ratio_np = np.asarray(ratio.data)
    clipfrac = masked_mean(
        (np.asarray(pg_losses2.data) > np.asarray(pg_losses1.data)).astype(np.float32), mask
    )
    lower = masked_mean(
        ((np.asarray(clip_pg_losses1.data) > np.asarray((-advantages * clip_ratio_c))) & (advantages < 0)).astype(
            np.float32
        ),
        mask,
    )
    return pg_loss, {
        "actor/pg_clipfrac": clipfrac,
        "actor/pg_clipfrac_lower": lower,
        "actor/ppo_kl": ppo_kl,
        "actor/ratio_mean": masked_mean(ratio_np, mask),
    }


def compute_policy_loss_cispo(
    old_log_prob: Tensor,
    log_prob: Tensor,
    advantages: np.ndarray,
    response_mask: np.ndarray,
    clip_ratio: float = 0.2,
    clip_ratio_low: float | None = None,
    clip_ratio_high: float | None = None,
    loss_agg_mode: str = "token-mean",
    dp_size: int = 1,
    batch_num_tokens: float | None = None,
    global_batch_size: float | None = None,
    loss_scale_factor: float | None = None,
    rollout_is_weights: np.ndarray | None = None,
) -> tuple[Tensor, dict[str, float]]:
    """CISPO: clip the importance weight, but keep the score-function gradient.

    The clipped ratio is detached, so it scales ``log pi`` without contributing
    a ratio-gradient term::

        L = -E[ sg(clip(rho, 1-eps_low, 1+eps_high)) * A * log pi ]

    This differs from PPO in two ways that matter for long reasoning traces:

    * there is no ``min`` between clipped and unclipped branches, so a large
      ratio cannot silently zero the gradient;
    * the gradient always flows through ``log pi`` (the score function), which
      keeps the update a policy gradient rather than a trust-region surrogate.

    The usual token/sequence aggregation still applies.  ``response_mask`` is
    applied by :func:`agg_loss`, exactly as in the vanilla loss.
    """
    low = clip_ratio if clip_ratio_low is None else clip_ratio_low
    high = clip_ratio if clip_ratio_high is None else clip_ratio_high
    advantages = np.asarray(advantages, dtype=np.float32)
    mask = np.asarray(response_mask, dtype=np.float32)

    negative_approx_kl = (log_prob - old_log_prob).clip(-20.0, 20.0)
    ratio = negative_approx_kl.exp()
    clipped_ratio = clip(ratio, 1.0 - low, 1.0 + high)
    # Stop-gradient on the weight; gradient reaches ``log_prob`` through the
    # third factor.  Multiplying by a NumPy array broadcasts the advantage.
    pg_losses = -clipped_ratio.detach() * advantages * log_prob
    if rollout_is_weights is not None:
        pg_losses = pg_losses * np.asarray(rollout_is_weights, dtype=np.float32)

    pg_loss = agg_loss(
        pg_losses,
        mask,
        loss_agg_mode,
        dp_size=dp_size,
        batch_num_tokens=batch_num_tokens,
        global_batch_size=global_batch_size,
        loss_scale_factor=loss_scale_factor,
    )
    ratio_np = np.asarray(ratio.data)
    clipfrac = masked_mean(
        ((ratio_np < 1.0 - low) | (ratio_np > 1.0 + high)).astype(np.float32), mask
    )
    return pg_loss, {
        "actor/cispo_ratio_mean": masked_mean(ratio_np, mask),
        "actor/cispo_clipfrac": clipfrac,
        "actor/ppo_kl": masked_mean(-np.asarray(negative_approx_kl.data), mask),
    }


def compute_policy_loss_gspo(
    old_log_prob: Tensor,
    log_prob: Tensor,
    advantages: np.ndarray,
    response_mask: np.ndarray,
    clip_ratio: float = 0.2,
    clip_ratio_low: float | None = None,
    clip_ratio_high: float | None = None,
    loss_agg_mode: str = "seq-mean-token-mean",
    dp_size: int = 1,
    batch_num_tokens: float | None = None,
    global_batch_size: float | None = None,
    loss_scale_factor: float | None = None,
    rollout_is_weights: np.ndarray | None = None,
) -> tuple[Tensor, dict[str, float]]:
    """Sequence-level importance ratio (GSPO).

    The ratio is constant within a sequence: ``exp(mean_t log pi/pi_old)``.
    """
    mask = np.asarray(response_mask, dtype=np.float32)
    lengths = np.maximum(mask.sum(axis=-1, keepdims=True), 1.0)
    negative_approx_kl = (log_prob - old_log_prob) * mask
    log_ratio = negative_approx_kl.sum(axis=-1, keepdims=True) / lengths
    # Match verl's GSPO gradient: the sequence ratio is a stop-gradient
    # constant, while the gradient flows through log pi.  Writing
    # ``exp(log_seq_ratio)`` directly would differentiate the ratio itself and
    # optimize a different objective.
    seq_const = np.exp(np.clip(log_ratio.data, -20.0, 20.0))
    token_ratio = (log_prob - log_prob.detach()).exp() * seq_const
    low = clip_ratio if clip_ratio_low is None else clip_ratio_low
    high = clip_ratio if clip_ratio_high is None else clip_ratio_high
    advantages = np.asarray(advantages, dtype=np.float32)
    unclipped = -token_ratio * advantages
    clipped = -clip(token_ratio, 1.0 - low, 1.0 + high) * advantages
    pg_losses = maximum(unclipped, clipped)
    if rollout_is_weights is not None:
        pg_losses = pg_losses * np.asarray(rollout_is_weights, dtype=np.float32)
    pg_loss = agg_loss(
        pg_losses,
        mask,
        loss_agg_mode,
        dp_size=dp_size,
        batch_num_tokens=batch_num_tokens,
        global_batch_size=global_batch_size,
        loss_scale_factor=loss_scale_factor,
    )
    return pg_loss, {"actor/gspo_ratio_mean": float(seq_const.mean())}


def compute_value_loss(
    vpreds: Tensor,
    returns: np.ndarray,
    values: np.ndarray,
    response_mask: np.ndarray,
    cliprange_value: float = 0.5,
    loss_agg_mode: str = "token-mean",
    dp_size: int = 1,
    batch_num_tokens: float | None = None,
    global_batch_size: float | None = None,
    loss_scale_factor: float | None = None,
) -> tuple[Tensor, float]:
    returns = np.asarray(returns, dtype=np.float32)
    values = np.asarray(values, dtype=np.float32)
    mask = np.asarray(response_mask, dtype=np.float32)
    vpredclipped = clip(vpreds, values - cliprange_value, values + cliprange_value)
    vf_losses1 = (vpreds - returns) ** 2
    vf_losses2 = (vpredclipped - returns) ** 2
    clipped_vf_losses = maximum(vf_losses1, vf_losses2)
    vf_loss = 0.5 * agg_loss(
        clipped_vf_losses,
        mask,
        loss_agg_mode,
        dp_size=dp_size,
        batch_num_tokens=batch_num_tokens,
        global_batch_size=global_batch_size,
        loss_scale_factor=loss_scale_factor,
    )
    clipfrac = masked_mean(
        (np.asarray(vf_losses2.data) > np.asarray(vf_losses1.data)).astype(np.float32), mask
    )
    return vf_loss, clipfrac


def compute_entropy_loss(
    entropy: Tensor,
    response_mask: np.ndarray,
    loss_agg_mode: str = "token-mean",
    **agg_kwargs,
) -> Tensor:
    return agg_loss(-entropy, np.asarray(response_mask, dtype=np.float32), loss_agg_mode, **agg_kwargs)


# ----------------------------------------------------------------------
# KL controllers
# ----------------------------------------------------------------------


class FixedKLController:
    def __init__(self, kl_coef: float):
        self.value = float(kl_coef)

    def update(self, current_kl: float, n_steps: int) -> None:
        return None


class AdaptiveKLController:
    """Piecewise KL controller from the original PPO/InstructGPT recipe."""

    def __init__(self, init_kl_coef: float, target_kl: float, horizon: int):
        self.value = float(init_kl_coef)
        self.target = float(target_kl)
        self.horizon = int(horizon)

    def update(self, current_kl: float, n_steps: int) -> None:
        proportional_error = np.clip(current_kl / max(self.target, 1e-8) - 1.0, -0.2, 0.2)
        self.value *= 1.0 + proportional_error * n_steps / max(self.horizon, 1)


def get_kl_controller(kind: str, kl_coef: float, horizon: int = 10000, target_kl: float = 0.01):
    if kind == "fixed":
        return FixedKLController(kl_coef)
    if kind == "adaptive":
        return AdaptiveKLController(kl_coef, target_kl, horizon)
    raise ValueError(f"unknown KL controller {kind!r}")
