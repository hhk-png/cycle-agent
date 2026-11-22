from __future__ import annotations

import numpy as np
import pytest

from mini_verl.algo.core_algos import (
    AdaptiveKLController,
    FixedKLController,
    agg_loss,
    compute_advantage,
    compute_entropy_loss,
    compute_gae_advantage_return,
    compute_grpo_outcome_advantage,
    compute_policy_loss,
    compute_policy_loss_cispo,
    compute_policy_loss_gspo,
    compute_reinforce_plus_plus_outcome_advantage,
    compute_remax_outcome_advantage,
    compute_rewards,
    compute_rloo_outcome_advantage,
    compute_value_loss,
    get_kl_controller,
    kl_penalty,
    masked_mean,
    masked_whiten,
)
from mini_verl.autograd import Tensor
from mini_verl.losses import actor_loss_fn
from mini_verl.nn import entropy_from_logits


class _FixedLogitModel:
    def __init__(self, logits: np.ndarray):
        self.logits = logits

    def __call__(self, input_ids):
        return Tensor(self.logits, requires_grad=True)


def test_masked_helpers():
    x = np.asarray([1.0, 2.0, 3.0])
    mask = np.asarray([1.0, 0.0, 1.0])
    assert masked_mean(x, mask) == 2.0
    whitened = masked_whiten(np.asarray([[1.0, 3.0]]), np.ones((1, 2)))
    assert abs(float(whitened.mean())) < 1e-5


def test_gae_matches_manual():
    rewards = np.asarray([[0.0, 0.0, 1.0, 0.0]], dtype=np.float32)
    values = np.asarray([[0.1, 0.2, 0.3, 0.4]], dtype=np.float32)
    mask = np.asarray([[0.0, 1.0, 1.0, 1.0]], dtype=np.float32)
    adv, ret = compute_gae_advantage_return(rewards, values, mask, gamma=1.0, lam=1.0)
    # reward 1 lands on t=2.  t=3: -0.4; t=2: 1.0-0.4=0.6; t=1: 0.1+0.6=0.7
    assert np.allclose(adv[0, 1:], [0.8, 0.7, -0.4], atol=1e-5)
    assert adv[0, 0] == 0.0
    assert np.allclose(ret[0, 1:], adv[0, 1:] + values[0, 1:])


def test_gae_padding_does_not_leak_value():
    rewards = np.asarray([[0.0, 1.0, 0.0]], dtype=np.float32)
    values = np.asarray([[0.0, 0.5, 99.0]], dtype=np.float32)
    mask = np.asarray([[0.0, 1.0, 0.0]], dtype=np.float32)
    adv, _ = compute_gae_advantage_return(rewards, values, mask, 1.0, 1.0)
    assert adv[0, 1] == pytest.approx(0.5)


def test_gae_freezes_across_interior_mask():
    # A masked tool observation inside the trajectory is still a time step, but
    # verl freezes lastgaelam/nextvalues there instead of applying gamma*lam.
    rewards = np.asarray([[0.0, 0.0, 1.0, 0.0, 0.0]], dtype=np.float32)
    values = np.asarray([[0.0, 0.1, 0.2, 0.3, 0.4]], dtype=np.float32)
    mask = np.asarray([[0.0, 1.0, 0.0, 1.0, 1.0]], dtype=np.float32)
    adv, _ = compute_gae_advantage_return(rewards, values, mask, gamma=0.9, lam=0.9)
    # t=4: -0.4; next=0.4
    # t=3: (0.9*0.4 - 0.3) + 0.81*(-0.4) = -0.264; next=0.3
    # t=2 (masked): last_gae stays -0.264, next stays 0.3
    # t=1: (0.9*0.3 - 0.1) + 0.81*(-0.264) = -0.04384
    assert adv[0, 4] == pytest.approx(-0.4, abs=1e-6)
    assert adv[0, 3] == pytest.approx(-0.264, abs=1e-6)
    assert adv[0, 2] == 0.0
    assert adv[0, 1] == pytest.approx(0.17 + 0.81 * -0.264, abs=1e-5)


def test_gae_bootstraps_truncated_trajectory():
    rewards = np.asarray([[0.0, 1.0]], dtype=np.float32)
    values = np.asarray([[0.2, 0.5]], dtype=np.float32)
    mask = np.ones((1, 2), dtype=np.float32)
    terminated, _ = compute_gae_advantage_return(rewards, values, mask, 1.0, 1.0)
    truncated, _ = compute_gae_advantage_return(
        rewards, values, mask, 1.0, 1.0, truncated=np.asarray([1])
    )
    assert terminated[0, 1] == pytest.approx(0.5)
    assert truncated[0, 1] == pytest.approx(1.0)


def test_grpo():
    rewards = np.zeros((4, 2), dtype=np.float32)
    rewards[:, -1] = [1.0, 0.0, 0.0, 0.0]
    mask = np.ones((4, 2), dtype=np.float32)
    adv, _ = compute_grpo_outcome_advantage(rewards, mask, np.asarray([7, 7, 7, 7]))
    # mean=.25, unbiased std=.5
    assert adv[0, 0] == pytest.approx(1.5, abs=1e-5)
    assert adv[1, 0] == pytest.approx(-0.5, abs=1e-5)


def test_grpo_singleton_group_uses_zero_baseline():
    rewards = np.zeros((1, 2), dtype=np.float32)
    rewards[0, -1] = 1.0
    adv, _ = compute_grpo_outcome_advantage(rewards, np.ones((1, 2)), np.asarray([1]))
    # verl's singleton branch sets mean=0, std=1, so the raw score survives.
    assert np.allclose(adv, 1.0)


def test_grpo_without_std_norm():
    rewards = np.zeros((2, 2), dtype=np.float32)
    rewards[:, -1] = [2.0, 0.0]
    adv, _ = compute_grpo_outcome_advantage(rewards, np.ones((2, 2)), np.asarray([1, 1]), norm_adv_by_std_in_grpo=False)
    assert adv[0, 0] == pytest.approx(1.0)
    assert adv[1, 0] == pytest.approx(-1.0)


def test_rloo():
    rewards = np.zeros((3, 2), dtype=np.float32)
    rewards[:, -1] = [3.0, 1.0, 2.0]
    adv, _ = compute_rloo_outcome_advantage(rewards, np.ones((3, 2)), np.asarray([1, 1, 1]))
    assert adv[0, 0] == pytest.approx(3.0 - 1.5)
    assert adv[1, 0] == pytest.approx(1.0 - 2.5)


def test_reinforce_plus_plus():
    rewards = np.zeros((1, 3), dtype=np.float32)
    rewards[0, 2] = 1.0
    adv, ret = compute_reinforce_plus_plus_outcome_advantage(rewards, np.ones((1, 3)), gamma=0.5)
    assert np.allclose(ret, [[0.25, 0.5, 1.0]])
    assert adv.shape == ret.shape


def test_reinforce_plus_plus_freezes_masked_step():
    rewards = np.zeros((1, 4), dtype=np.float32)
    rewards[0, 3] = 1.0
    mask = np.asarray([[1.0, 0.0, 1.0, 1.0]], dtype=np.float32)
    _, ret = compute_reinforce_plus_plus_outcome_advantage(rewards, mask, gamma=0.5)
    # Position 1 is a tool observation: it contributes no return and freezes
    # the running return, so position 0 sees 0.5 * 0.5 = 0.25 (not 0.125).
    assert ret[0, 0] == pytest.approx(0.25)
    assert ret[0, 1] == 0.0
    assert ret[0, 2] == pytest.approx(0.5)
    assert ret[0, 3] == pytest.approx(1.0)


def test_remax():
    rewards = np.zeros((2, 3), dtype=np.float32)
    rewards[0, 2] = 1.0
    rewards[1, 2] = 2.0
    adv, ret = compute_remax_outcome_advantage(rewards, np.asarray([0.5, 0.5]), np.ones((2, 3)))
    assert adv[0, 2] == pytest.approx(0.5)
    assert adv[1, 2] == pytest.approx(1.5)


@pytest.mark.parametrize(
    "kind,expected",
    [
        ("kl", 0.5),
        ("abs", 0.5),
        ("mse", 0.125),
    ],
)
def test_kl_penalty_basic(kind, expected):
    logp = np.asarray([-1.0], dtype=np.float32)
    ref = np.asarray([-1.5], dtype=np.float32)
    assert kl_penalty(logp, ref, kind)[0] == pytest.approx(expected, abs=1e-5)


def test_kl_penalty_k3():
    logp = np.asarray([-1.0], dtype=np.float32)
    ref = np.asarray([-1.5], dtype=np.float32)
    value = kl_penalty(logp, ref, "low_var_kl")[0]
    assert np.isfinite(value)
    assert value >= -10


def test_compute_rewards_subtracts_kl():
    scores = np.asarray([[0.0, 1.0]], dtype=np.float32)
    old = np.asarray([[0.0, -1.0]], dtype=np.float32)
    ref = np.asarray([[0.0, -2.0]], dtype=np.float32)
    rewards = compute_rewards(scores, old, ref, 0.5)
    assert rewards[0, 1] == pytest.approx(1.0 - 0.5)


@pytest.mark.parametrize(
    "mode,expected",
    [
        ("token-mean", 2.0),
        ("token-sum", 6.0),
        ("seq-mean-token-sum", 3.0),
        ("seq-mean-token-mean", 2.25),
        ("seq-mean-token-sum-norm", 0.75),
    ],
)
def test_agg_loss_modes(mode, expected):
    loss = np.asarray([[1.0, 2.0, 0.0], [3.0, 0.0, 0.0]], dtype=np.float32)
    mask = np.asarray([[1.0, 1.0, 0.0], [1.0, 0.0, 0.0]], dtype=np.float32)
    value = agg_loss(loss, mask, mode, loss_scale_factor=4)
    assert float(value) == pytest.approx(expected)


def test_policy_loss_no_change():
    old = Tensor(np.zeros((1, 3), dtype=np.float32))
    new = Tensor(np.zeros((1, 3), dtype=np.float32))
    adv = np.ones((1, 3), dtype=np.float32)
    mask = np.ones((1, 3), dtype=np.float32)
    loss, metrics = compute_policy_loss(old, new, adv, mask, clip_ratio=0.2)
    assert loss.item() == pytest.approx(-1.0)
    assert metrics["actor/pg_clipfrac"] == 0.0


def test_policy_loss_clips_positive_advantage():
    old = Tensor(np.zeros((1, 1), dtype=np.float32))
    new = Tensor(np.full((1, 1), 10.0, dtype=np.float32))
    adv = np.ones((1, 1), dtype=np.float32)
    mask = np.ones((1, 1), dtype=np.float32)
    loss, metrics = compute_policy_loss(old, new, adv, mask, clip_ratio=0.2)
    assert loss.item() == pytest.approx(-1.2)
    assert metrics["actor/pg_clipfrac"] == 1.0


def test_policy_loss_dual_clip_negative_advantage():
    old = Tensor(np.zeros((1, 1), dtype=np.float32))
    new = Tensor(np.full((1, 1), 10.0, dtype=np.float32))
    adv = -np.ones((1, 1), dtype=np.float32)
    mask = np.ones((1, 1), dtype=np.float32)
    loss, metrics = compute_policy_loss(old, new, adv, mask, clip_ratio=0.2, clip_ratio_c=3.0)
    # The dual-clip lower bound caps the loss at clip_ratio_c * |A| = 3.
    assert loss.item() == pytest.approx(3.0)
    assert metrics["actor/pg_clipfrac_lower"] == 1.0


def test_policy_loss_clip_higher():
    old = Tensor(np.zeros((1, 1), dtype=np.float32))
    new = Tensor(np.full((1, 1), 10.0, dtype=np.float32))
    adv = np.ones((1, 1), dtype=np.float32)
    mask = np.ones((1, 1), dtype=np.float32)
    loss, _ = compute_policy_loss(old, new, adv, mask, clip_ratio_low=0.2, clip_ratio_high=0.5)
    assert loss.item() == pytest.approx(-1.5)


def test_cispo_clips_weight_but_keeps_score_gradient():
    old = Tensor(np.zeros((1, 1), dtype=np.float32))
    new = Tensor(np.full((1, 1), 10.0, dtype=np.float32), requires_grad=True)
    adv = np.ones((1, 1), dtype=np.float32)
    mask = np.ones((1, 1), dtype=np.float32)
    loss, metrics = compute_policy_loss_cispo(old, new, adv, mask, clip_ratio=0.2)
    # rho = e^10 is clipped to 1.2; unlike PPO there is no min() branch, so
    # the loss is -1.2 * log pi = -12, and the gradient w.r.t. log pi is -1.2.
    assert loss.item() == pytest.approx(-12.0)
    assert metrics["actor/cispo_clipfrac"] == 1.0
    loss.backward()
    assert new.grad == pytest.approx(np.asarray([[-1.2]], dtype=np.float32))


def test_cispo_respects_mask_and_global_token_normalization():
    old = Tensor(np.zeros((1, 2), dtype=np.float32))
    new = Tensor(np.full((1, 2), 10.0, dtype=np.float32))
    adv = np.ones((1, 2), dtype=np.float32)
    mask = np.asarray([[1.0, 0.0]], dtype=np.float32)
    loss, _ = compute_policy_loss_cispo(
        old, new, adv, mask, clip_ratio=0.2, batch_num_tokens=2.0
    )
    # Only one valid token: -1.2 * 10 / 2 = -6.  The padded token must not
    # contribute, and the denominator is the global token count.
    assert loss.item() == pytest.approx(-6.0)


def test_gspo_sequence_ratio():
    old = Tensor(np.zeros((2, 3), dtype=np.float32))
    new = Tensor(np.zeros((2, 3), dtype=np.float32))
    adv = np.asarray([[1.0, 1.0, 1.0], [-1.0, -1.0, -1.0]], dtype=np.float32)
    mask = np.ones((2, 3), dtype=np.float32)
    loss, metrics = compute_policy_loss_gspo(old, new, adv, mask)
    assert loss.item() == pytest.approx(0.0, abs=1e-6)
    assert "actor/gspo_ratio_mean" in metrics


def test_value_loss():
    pred = Tensor(np.asarray([[1.0, 2.0]], dtype=np.float32))
    returns = np.asarray([[1.0, 1.0]], dtype=np.float32)
    old = np.asarray([[1.0, 2.0]], dtype=np.float32)
    mask = np.ones((1, 2), dtype=np.float32)
    loss, clipfrac = compute_value_loss(pred, returns, old, mask, 0.5)
    assert loss.item() == pytest.approx(0.25)
    assert clipfrac == 0.0


def test_entropy_loss():
    logits = Tensor(np.zeros((1, 2, 4), dtype=np.float32))
    mask = np.ones((1, 2), dtype=np.float32)
    entropy = entropy_from_logits(logits, mask)
    loss = compute_entropy_loss(entropy, mask)
    assert loss.item() == pytest.approx(-np.log(4), abs=1e-5)


def test_actor_entropy_uses_shifted_mask():
    # Only the token at position 3 is a response token.  It is predicted by
    # logits at position 2; position 3's logits must NOT contribute.
    logits = np.zeros((1, 4, 4), dtype=np.float32)
    logits[0, 2, :] = 0.0          # uniform -> entropy log(4)
    logits[0, 3, 0] = 100.0        # deterministic -> entropy 0
    model = _FixedLogitModel(logits)
    batch = {
        "input_ids": np.asarray([[1, 2, 0, 3]], dtype=np.int64),
        "response_mask": np.asarray([[0.0, 0.0, 0.0, 1.0]], dtype=np.float32),
        "old_log_probs": np.zeros((1, 4), dtype=np.float32),
        "advantages": np.zeros((1, 4), dtype=np.float32),
        "calculate_entropy": True,
        "entropy_coeff": 0.0,
    }
    _, metrics = actor_loss_fn(model, batch)
    assert metrics["actor/entropy"] == pytest.approx(np.log(4), abs=1e-5)


def test_differentiable_abs_kl_loss():
    logits = np.zeros((1, 3, 4), dtype=np.float32)
    model = _FixedLogitModel(logits)
    batch = {
        "input_ids": np.asarray([[1, 2, 3]], dtype=np.int64),
        "response_mask": np.asarray([[0.0, 1.0, 1.0]], dtype=np.float32),
        "old_log_probs": np.zeros((1, 3), dtype=np.float32),
        "ref_log_prob": np.full((1, 3), -0.5, dtype=np.float32),
        "advantages": np.zeros((1, 3), dtype=np.float32),
        "use_kl_loss": True,
        "kl_loss_type": "abs",
        "kl_loss_coef": 0.1,
        "calculate_entropy": False,
    }
    loss, metrics = actor_loss_fn(model, batch)
    loss.backward()
    assert np.isfinite(loss.item())
    assert "actor/kl_loss" in metrics


def test_entropy_coefficient_is_a_bonus_not_a_penalty():
    logits = np.zeros((1, 4, 4), dtype=np.float32)
    model = _FixedLogitModel(logits)
    batch = {
        "input_ids": np.asarray([[1, 2, 0, 3]], dtype=np.int64),
        "response_mask": np.asarray([[0.0, 0.0, 0.0, 1.0]], dtype=np.float32),
        "old_log_probs": np.zeros((1, 4), dtype=np.float32),
        "advantages": np.zeros((1, 4), dtype=np.float32),
        "calculate_entropy": True,
        "entropy_coeff": 0.1,
    }
    loss, metrics = actor_loss_fn(model, batch)
    # pg_loss = 0; entropy_loss = -H; the bonus term must be -0.1 * H.
    assert loss.item() == pytest.approx(-0.1 * np.log(4), abs=1e-5)
    assert metrics["actor/entropy"] == pytest.approx(np.log(4), abs=1e-5)


def test_kl_controllers():
    fixed = FixedKLController(0.1)
    fixed.update(99, 1)
    assert fixed.value == 0.1
    adaptive = AdaptiveKLController(0.1, 1.0, 10)
    adaptive.update(2.0, 1)
    assert adaptive.value > 0.1
    assert isinstance(get_kl_controller("fixed", 0.1), FixedKLController)
    assert isinstance(get_kl_controller("adaptive", 0.1), AdaptiveKLController)


def test_compute_advantage_dispatch():
    rewards = np.zeros((2, 2), dtype=np.float32)
    rewards[:, -1] = [1.0, 0.0]
    mask = np.ones((2, 2), dtype=np.float32)
    adv, _ = compute_advantage("grpo", rewards, mask, index=np.asarray([1, 1]))
    assert adv.shape == (2, 2)
    with pytest.raises(ValueError):
        compute_advantage("nope", rewards, mask)
