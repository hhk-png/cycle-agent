"""Regression tests for the second independent MiniVerl audit.

Each test pins a behavior that was previously silent (false green):
IS weights dropped by GSPO/CISPO, rollout correction disabled in sync mode,
missing final weight sync, staleness counted in sync versions, unwired LR
scheduler, DAPO shaping leaking into evaluation, math prompt truncation, DPO
mean-vs-sum log-probs, RNG state on resume, and clipped softplus gradients.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import numpy as np
import pytest

from mini_verl.algo.core_algos import (
    compute_policy_loss_cispo,
    compute_policy_loss_gspo,
)
from mini_verl.autograd import Tensor
from mini_verl.data import (
    CharTokenizer,
    build_dataset,
    collate_fn,
    preference_pairs,
)
from mini_verl.losses import (
    _sequence_logprob,
    _softplus,
    distillation_loss_fn,
    dpo_loss_fn,
)
from mini_verl.nn import TinyLinearLM, log_probs_from_logits
from mini_verl.trainer.ppo_trainer import PPOTrainer


def _loss_inputs(rows: int = 2, cols: int = 3):
    old = Tensor(np.zeros((rows, cols), dtype=np.float32))
    new = Tensor(np.zeros((rows, cols), dtype=np.float32), requires_grad=True)
    advantages = np.ones((rows, cols), dtype=np.float32)
    mask = np.ones((rows, cols), dtype=np.float32)
    return old, new, advantages, mask


def test_is_weights_apply_to_cispo_and_gspo():
    old, new, advantages, mask = _loss_inputs()
    zero = np.zeros((2, 3), dtype=np.float32)
    one = np.ones((2, 3), dtype=np.float32)

    base_cispo, _ = compute_policy_loss_cispo(old, new, advantages, mask)
    zero_cispo, _ = compute_policy_loss_cispo(
        old, new, advantages, mask, rollout_is_weights=zero
    )
    one_cispo, _ = compute_policy_loss_cispo(
        old, new, advantages, mask, rollout_is_weights=one
    )
    assert zero_cispo.item() == 0.0
    assert one_cispo.item() == pytest.approx(base_cispo.item())

    base_gspo, _ = compute_policy_loss_gspo(old, new, advantages, mask)
    zero_gspo, _ = compute_policy_loss_gspo(
        old, new, advantages, mask, rollout_is_weights=zero
    )
    one_gspo, _ = compute_policy_loss_gspo(
        old, new, advantages, mask, rollout_is_weights=one
    )
    assert zero_gspo.item() == 0.0
    assert one_gspo.item() == pytest.approx(base_gspo.item())


def test_rollout_correction_applies_in_sync_mode(fast_config):
    config = fast_config(
        trainer__total_training_steps=1,
        algorithm__rollout_correction__enable=True,
        algorithm__rollout_correction__bypass_mode=False,
    )
    trainer = PPOTrainer(config).init()
    metrics = trainer.train_step()
    assert "rollout_is/weight_mean" in metrics
    assert np.isfinite(metrics["rollout_is/weight_mean"])


def test_final_fit_syncs_rollout_copy(fast_config):
    config = fast_config(
        trainer__total_training_steps=4,
        trainer__parameter_sync_step=3,
        trainer__test_freq=0,
    )
    trainer = PPOTrainer(config).init()
    trainer.fit()
    rollout_state = (
        trainer.rollout.model.state_dict()
        if trainer.rollout.model is not None
        else trainer.rollout._backup
    )
    actor_state = trainer.actor_group.get_actor_state_dict()
    for key, value in actor_state.items():
        assert np.allclose(value, rollout_state[key]), key


def test_async_staleness_counts_actor_updates(fast_config):
    config = fast_config(
        trainer__trainer_mode="colocate_async",
        trainer__total_training_steps=5,
        trainer__parameter_sync_step=3,
        trainer__test_freq=0,
    )
    trainer = PPOTrainer(config).init()
    result = trainer.fit()
    # With S=3 the rollout weights lag the actor by up to 2 updates; a
    # sync-version counter would report at most 1.  history stores the running
    # mean of per-step maxima, so the aggregate is between 1 and 2.
    assert result["history"]["async/staleness_max"] > 1.0


def test_lr_scheduler_horizon_is_wired(fast_config):
    config = fast_config(
        trainer__total_training_steps=4,
        actor_rollout_ref__actor__optim__lr_scheduler_type="cosine",
        actor_rollout_ref__actor__optim__lr=0.01,
    )
    trainer = PPOTrainer(config).init()
    lrs = [trainer.train_step()["lr"] for _ in range(4)]
    assert lrs[0] > lrs[-1]


def test_evaluate_strips_training_shaping(fast_config):
    from mini_verl.rewards import DAPORewardManager, RewardManager

    shaped = DAPORewardManager("exact", overlong_penalty_factor=0.5)
    naive = RewardManager(shaped.reward_fn)
    assert shaped.score(
        "x", "x", response_length=4, max_length=4, truncated=True
    ) < 1.0
    assert naive.score(
        "x", "x", response_length=4, max_length=4, truncated=True
    ) == 1.0

    config = fast_config(
        trainer__total_training_steps=1,
        algorithm__overlong_penalty_factor=0.5,
        reward__reward_manager="dapo",
    )
    trainer = PPOTrainer(config).init()
    assert trainer.reward_manager.overlong_penalty_factor == 0.5


def test_math_prompt_keeps_operands_under_truncation():
    tokenizer = CharTokenizer()
    dataset = build_dataset("math", 2, seed=0)
    batch = collate_fn(dataset.samples, tokenizer, 24, 8)
    for row, length in enumerate(batch.batch["prompt_length"]):
        text = tokenizer.decode(batch.batch["input_ids"][row, : int(length)])
        assert any(char.isdigit() for char in text), text


def test_dpo_uses_sum_logprob_and_compacts_holes():
    tokenizer = CharTokenizer()
    model = TinyLinearLM(vocab_size=tokenizer.vocab_size, max_seq_len=32, dim=8, seed=0)
    prompt = np.asarray([[tokenizer.stoi["a"]]], dtype=np.int64)
    response = np.asarray([[tokenizer.stoi["x"], tokenizer.stoi["y"]]], dtype=np.int64)
    mask = np.ones((1, 2), dtype=np.float32)

    got = _sequence_logprob(model, prompt, response, mask).item()
    sequence = np.concatenate([prompt[0], response[0]])[None, :]
    full_mask = np.asarray([[0.0, 1.0, 1.0]], dtype=np.float32)
    logits = model(sequence)
    manual = log_probs_from_logits(logits, sequence, full_mask).data.sum()
    assert got == pytest.approx(float(manual), rel=1e-5)

    # A hole in the mask must compact the selected tokens, not drop the tail.
    holed = np.asarray([[1.0, 0.0, 1.0]], dtype=np.float32)
    response3 = np.asarray([[tokenizer.stoi["x"], tokenizer.stoi["y"], tokenizer.stoi["z"]]])
    compact = _sequence_logprob(model, prompt, response3, holed)
    expected_seq = np.concatenate([prompt[0], response3[0][[0, 2]]])[None, :]
    expected_mask = np.asarray([[0.0, 1.0, 1.0]], dtype=np.float32)
    expected = log_probs_from_logits(
        model(expected_seq), expected_seq, expected_mask
    ).data.sum()
    assert compact.item() == pytest.approx(float(expected), rel=1e-5)

    pairs = preference_pairs(build_dataset("bandit", 2, seed=0), tokenizer, 4, 2)
    loss, _ = dpo_loss_fn(model, dict(pairs[0].batch), beta=0.1)
    assert np.isfinite(loss.item())


def test_softplus_is_stable_and_has_gradient_at_extremes():
    x = Tensor(np.asarray([-30.0, 30.0], dtype=np.float32), requires_grad=True)
    y = _softplus(x)
    assert y.data[0] == pytest.approx(0.0, abs=1e-6)
    assert y.data[1] == pytest.approx(30.0, abs=1e-5)
    y.backward(np.ones_like(y.data))
    assert x.grad[1] > 0.5  # clipping used to make this exactly 0


def test_distillation_uses_global_token_normalization():
    rng = np.random.default_rng(0)
    student = rng.standard_normal((6, 2, 5)).astype(np.float32)
    teacher = rng.standard_normal((6, 2, 5)).astype(np.float32)
    mask = np.ones((6, 2), dtype=np.float32)

    full, _ = distillation_loss_fn(Tensor(student), teacher, mask)
    # Split 2 + 4 tokens across two DP ranks; each local loss is scaled by
    # dp_size and divided by the global token count.  The mean must recover the
    # full-batch loss.
    a, _ = distillation_loss_fn(
        Tensor(student[:2]), teacher[:2], mask[:2], dp_size=2, batch_num_tokens=12
    )
    b, _ = distillation_loss_fn(
        Tensor(student[2:]), teacher[2:], mask[2:], dp_size=2, batch_num_tokens=12
    )
    assert (a.item() + b.item()) / 2 == pytest.approx(full.item(), rel=1e-5)


def test_resume_restores_rollout_rng(fast_config, tmp_path):
    config = fast_config(trainer__total_training_steps=1, trainer__test_freq=0)
    config.trainer.log_dir = str(tmp_path)
    trainer = PPOTrainer(config).init()
    trainer.train_step()
    checkpoint = Path(trainer.save_checkpoint())
    state = json.loads((checkpoint / "trainer_state.json").read_text(encoding="utf-8"))
    assert "rollout_rng" in state

    resume_config = copy.deepcopy(config)
    resume_config.trainer.resume_from_path = str(checkpoint)
    resumed = PPOTrainer(resume_config).init()
    assert resumed.rollout.rng.bit_generator.state == state["rollout_rng"]
