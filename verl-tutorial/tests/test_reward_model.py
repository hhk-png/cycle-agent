from __future__ import annotations

import numpy as np
import pytest

from mini_verl.autograd import Tensor, log_sigmoid, no_grad, sigmoid, softplus
from mini_verl.config import PPOConfig
from mini_verl.data import CharTokenizer, build_dataset, collate_fn
from mini_verl.protocol import DataProto
from mini_verl.reward_model import (
    ModelRewardManager,
    RMTrainer,
    TinyRewardModel,
    bradley_terry_loss,
    build_preference_pairs,
    collate_preference_pairs,
    expected_calibration_error,
    load_reward_model,
    make_preference_batches,
    pairwise_metrics,
)
from mini_verl.trainer.ppo_trainer import PPOTrainer


def _finite_difference(function, value: np.ndarray, eps: float = 1e-2) -> np.ndarray:
    """Elementwise derivative of ``sum(function(value))``."""
    numeric = np.zeros_like(value, dtype=np.float64)
    for index in range(value.size):
        plus = value.astype(np.float64).copy()
        minus = value.astype(np.float64).copy()
        plus[index] += eps
        minus[index] -= eps
        numeric[index] = (function(plus) - function(minus)) / (2 * eps)
    return numeric


def _rm_config(tmp_path, *, total_steps: int = 40, arch: str = "mlp") -> PPOConfig:
    config = PPOConfig()
    config.trainer.logger = []
    config.trainer.seed = 3
    config.trainer.log_dir = str(tmp_path)
    config.trainer.experiment_name = "rm_unit"
    config.data.dataset = "bandit"
    config.data.train_size = 64
    config.data.val_size = 16
    config.data.train_batch_size = 16
    config.data.max_prompt_length = 16
    config.data.max_response_length = 8
    config.reward.reward_model = {
        "enable": True,
        "arch": arch,
        "hidden_size": 32,
        "pooling": "mean" if arch != "transformer" else "last",
        "lr": 0.02,
        "train_batch_size": 16,
        "total_steps": total_steps,
        "eval_freq": 10,
        "target_accuracy": 0.99,
        "patience": 4,
    }
    return config


# ----------------------------------------------------------------------
# autograd primitives
# ----------------------------------------------------------------------


def test_stable_ops_match_reference():
    values = np.asarray([-50.0, -3.0, -1.0, 0.0, 1.0, 3.0, 50.0], dtype=np.float32)
    sig = sigmoid(Tensor(values)).data
    sp = softplus(Tensor(values)).data
    ls = log_sigmoid(Tensor(values)).data
    assert np.all(np.isfinite(sig)) and np.all(np.isfinite(sp)) and np.all(np.isfinite(ls))
    assert np.allclose(sig, 1.0 / (1.0 + np.exp(-values)), atol=1e-6, rtol=1e-6)
    assert np.allclose(sp, np.logaddexp(0.0, values), atol=1e-5, rtol=1e-6)
    assert np.allclose(ls, -np.logaddexp(0.0, -values), atol=1e-5, rtol=1e-6)
    # Extreme negative logits must remain finite; log(sigmoid(x)) would be -inf.
    assert np.isfinite(log_sigmoid(Tensor(np.asarray([-500.0], dtype=np.float32))).data).all()


def test_stable_ops_gradients():
    values = np.asarray([-2.0, -0.5, 0.0, 0.5, 2.0], dtype=np.float32)
    for function in (sigmoid, softplus, log_sigmoid):
        tensor = Tensor(values.copy(), requires_grad=True)
        function(tensor).sum().backward()
        numeric = _finite_difference(
            lambda array: float(function(Tensor(array.astype(np.float32))).data.sum()),
            values.astype(np.float64),
        )
        assert np.allclose(tensor.grad, numeric, atol=1e-3, rtol=1e-3)


def test_softplus_gradient_is_sigmoid_everywhere():
    values = np.asarray([-80.0, -1.0, 0.0, 1.0, 80.0], dtype=np.float32)
    tensor = Tensor(values.copy(), requires_grad=True)
    softplus(tensor).sum().backward()
    expected = sigmoid(Tensor(values)).data
    assert np.allclose(tensor.grad, expected, atol=1e-6)


# ----------------------------------------------------------------------
# Bradley-Terry loss and metrics
# ----------------------------------------------------------------------


def test_bradley_terry_loss_direction_and_stability():
    correct = bradley_terry_loss(
        Tensor(np.asarray([1.0, 2.0], dtype=np.float32)),
        Tensor(np.asarray([0.0, 0.0], dtype=np.float32)),
    )
    wrong = bradley_terry_loss(
        Tensor(np.asarray([0.0, 0.0], dtype=np.float32)),
        Tensor(np.asarray([1.0, 2.0], dtype=np.float32)),
    )
    assert correct.item() < wrong.item()
    # z = 0 gives -log(sigmoid(0)) = log(2).
    assert bradley_terry_loss(Tensor(np.zeros(3)), Tensor(np.zeros(3))).item() == pytest.approx(
        np.log(2.0), abs=1e-6
    )
    extreme = bradley_terry_loss(Tensor(np.asarray([-500.0])), Tensor(np.asarray([500.0])))
    assert np.isfinite(extreme.item()) and extreme.item() > 900


def test_bradley_terry_margin_and_label_smoothing():
    chosen = Tensor(np.zeros(4))
    rejected = Tensor(np.zeros(4))
    base = bradley_terry_loss(chosen, rejected).item()
    margined = bradley_terry_loss(chosen, rejected, margin=1.0).item()
    smoothed = bradley_terry_loss(chosen, rejected, label_smoothing=0.2).item()
    assert margined > base
    # Symmetric smoothing at z=0 cannot change the loss.
    assert smoothed == pytest.approx(base, abs=1e-6)
    with pytest.raises(ValueError):
        bradley_terry_loss(chosen, rejected, label_smoothing=1.0)


def test_pairwise_metrics_and_calibration():
    metrics = pairwise_metrics(
        np.asarray([1.0, 2.0, -1.0], dtype=np.float32),
        np.asarray([0.0, 1.0, 0.0], dtype=np.float32),
    )
    assert metrics["accuracy"] == pytest.approx(2 / 3)
    assert metrics["margin"] == pytest.approx((1 + 1 - 1) / 3)
    assert 0.0 < metrics["prob"] < 1.0
    assert metrics["loss"] > 0
    assert expected_calibration_error(np.ones(8), np.ones(8)) == pytest.approx(0.0)
    assert expected_calibration_error(np.zeros(8), np.ones(8)) == pytest.approx(1.0)


# ----------------------------------------------------------------------
# model and data contract
# ----------------------------------------------------------------------


def test_pooling_last_ignores_earlier_tokens_but_mean_does_not():
    rng = np.random.default_rng(0)
    vocab = 16
    first = np.asarray([[2, 3, 4, 5]], dtype=np.int64)
    second = np.asarray([[2, 7, 8, 5]], dtype=np.int64)
    mask = np.ones_like(first, dtype=np.float32)
    last_model = TinyRewardModel(vocab, 8, arch="linear", dim=16, pooling="last", seed=0)
    mean_model = TinyRewardModel(vocab, 8, arch="linear", dim=16, pooling="mean", seed=0)
    with no_grad():
        last_scores = [last_model(ids, mask).item() for ids in (first, second)]
        mean_scores = [mean_model(ids, mask).item() for ids in (first, second)]
    assert last_scores[0] == pytest.approx(last_scores[1], abs=1e-7)
    assert mean_scores[0] != pytest.approx(mean_scores[1], abs=1e-7)


def test_pooling_respects_attention_mask():
    model = TinyRewardModel(16, 8, arch="linear", dim=16, pooling="mean", seed=0)
    ids = np.asarray([[2, 3, 4, 5]], dtype=np.int64)
    full = np.ones_like(ids, dtype=np.float32)
    truncated = full.copy()
    truncated[0, -2:] = 0.0
    with no_grad():
        assert model(ids, full).item() != pytest.approx(model(ids, truncated).item(), abs=1e-7)
    with pytest.raises(ValueError):
        model(ids, np.ones((1, 3), dtype=np.float32))


def test_preference_pairs_and_batches_pad_without_scoring_padding():
    tokenizer = CharTokenizer()
    dataset = build_dataset("bandit", 32, seed=0)
    pairs = build_preference_pairs(dataset, tokenizer, max_prompt_length=4, max_response_length=2, seed=0)
    assert len(pairs) == 32
    assert all(pair.chosen_ids != pair.rejected_ids for pair in pairs)
    batches = make_preference_batches(pairs, 8, tokenizer.pad_token_id, shuffle=False, seed=0)
    batch = batches[0]
    assert batch.chosen_ids.shape[0] == 8
    assert batch.chosen_attention.sum(axis=1).tolist() == [
        float(len(pair.prompt_ids) + len(pair.chosen_ids)) for pair in pairs[:8]
    ]
    pad_positions = batch.chosen_attention == 0
    assert np.all(batch.chosen_ids[pad_positions] == tokenizer.pad_token_id)
    # Padding positions must not change the score.
    model = TinyRewardModel(tokenizer.vocab_size, 16, arch="linear", dim=16, pooling="mean", seed=0)
    with no_grad():
        base = model(batch.chosen_ids, batch.chosen_attention).data
    changed = batch.chosen_ids.copy()
    changed[pad_positions] = 7
    with no_grad():
        assert np.allclose(base, model(changed, batch.chosen_attention).data)


# ----------------------------------------------------------------------
# training, checkpoint and serving
# ----------------------------------------------------------------------


def test_rm_training_improves_pairwise_accuracy(tmp_path):
    config = _rm_config(tmp_path, total_steps=40)
    trainer = RMTrainer(config)
    result = trainer.fit()
    assert result["val_accuracy"] >= 0.9
    assert result["val_ece"] < 0.2
    assert result["val_margin"] > 0


def test_rm_checkpoint_roundtrip_is_identical(tmp_path):
    config = _rm_config(tmp_path, total_steps=15)
    trainer = RMTrainer(config)
    trainer.fit()
    directory = trainer.save(tmp_path / "rm")
    loaded, metadata = load_reward_model(directory)
    assert metadata["arch"] == "mlp"
    tokenizer = CharTokenizer()
    pairs = build_preference_pairs(build_dataset("bandit", 16, 0), tokenizer, 16, 8, seed=4)
    batch = collate_preference_pairs(pairs, tokenizer.pad_token_id)
    with no_grad():
        before = trainer.model(batch.chosen_ids, batch.chosen_attention).data
        after = loaded(batch.chosen_ids, batch.chosen_attention).data
    assert np.allclose(before, after, atol=1e-6)


def test_model_reward_manager_scatters_to_last_response_token(tmp_path):
    config = _rm_config(tmp_path, total_steps=15)
    trainer = RMTrainer(config)
    trainer.fit()
    directory = trainer.save(tmp_path / "rm")
    tokenizer = CharTokenizer()
    dataset = build_dataset("bandit", 3, seed=0)
    data = collate_fn(dataset.samples, tokenizer, 4, 2)
    manager = ModelRewardManager(directory)
    out = manager.run(data)
    assert out.batch["rm_scores"].shape == (3,)
    for row in range(3):
        valid = np.flatnonzero(out.batch["response_mask"][row] > 0)
        assert out.batch["token_level_rewards"][row].sum() == pytest.approx(
            out.batch["rm_scores"][row]
        )
        assert out.batch["token_level_rewards"][row, int(valid[-1])] == pytest.approx(
            out.batch["rm_scores"][row]
        )
    assert out.meta_info["rm/model_path"].endswith("rm")


def test_model_reward_manager_requires_input_ids(tmp_path):
    config = _rm_config(tmp_path, total_steps=5)
    trainer = RMTrainer(config)
    trainer.fit()
    directory = trainer.save(tmp_path / "rm")
    manager = ModelRewardManager(directory)
    data = DataProto(batch={"response_mask": np.ones((1, 2), dtype=np.float32)})
    with pytest.raises(KeyError):
        manager.run(data)


def test_model_reward_manager_without_shaping(tmp_path):
    config = _rm_config(tmp_path, total_steps=5)
    trainer = RMTrainer(config)
    trainer.fit()
    directory = trainer.save(tmp_path / "rm")
    tokenizer = CharTokenizer()
    dataset = build_dataset("bandit", 1, seed=0)
    data = collate_fn(dataset.samples, tokenizer, 4, 2)
    data.batch["truncated"] = np.asarray([1], dtype=np.int64)
    manager = ModelRewardManager(directory, overlong_penalty_factor=1.0)
    penalized = manager.run(data.copy()).batch["rm_scores"][0]
    plain = manager.without_shaping().run(data.copy()).batch["rm_scores"][0]
    assert plain > penalized


def test_ppo_trainer_accepts_model_reward(tmp_path):
    config = _rm_config(tmp_path, total_steps=20)
    trainer = RMTrainer(config)
    trainer.fit()
    directory = trainer.save(tmp_path / "rm")

    ppo = PPOConfig()
    ppo.trainer.logger = []
    ppo.trainer.val_before_train = False
    ppo.trainer.total_training_steps = 4
    ppo.trainer.log_dir = str(tmp_path / "ppo")
    ppo.trainer.experiment_name = "rm_ppo"
    ppo.data.dataset = "bandit"
    ppo.data.train_size = 32
    ppo.data.val_size = 8
    ppo.data.train_batch_size = 8
    ppo.data.max_prompt_length = 4
    ppo.data.max_response_length = 1
    ppo.actor_rollout_ref.rollout.n = 4
    ppo.actor_rollout_ref.actor.ppo_mini_batch_size = 32
    ppo.reward.reward_model = {"enable": True, "model_path": str(directory)}
    result = PPOTrainer(ppo).fit()
    assert result["steps"] == 4
    assert np.isfinite(result["history"]["train/reward_mean"])


def test_ppo_missing_reward_model_path_fails_early(tmp_path):
    config = PPOConfig()
    config.trainer.logger = []
    config.trainer.val_before_train = False
    config.trainer.total_training_steps = 1
    config.trainer.log_dir = str(tmp_path)
    config.data.train_size = 16
    config.data.train_batch_size = 8
    config.data.max_prompt_length = 4
    config.data.max_response_length = 1
    config.actor_rollout_ref.rollout.n = 2
    config.actor_rollout_ref.actor.ppo_mini_batch_size = 16
    config.reward.reward_model = {"enable": True}
    with pytest.raises(ValueError, match="model_path"):
        PPOTrainer(config).init()
