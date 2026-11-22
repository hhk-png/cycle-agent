from __future__ import annotations

import json

import numpy as np
import pytest

from mini_verl.autograd import Tensor
from mini_verl.config import PPOConfig
from mini_verl.data import CharTokenizer, build_dataset
from mini_verl.prm import (
    AGGREGATIONS,
    ProcessRewardManager,
    PRMTrainer,
    StepSample,
    aggregate_step_probs,
    build_step_samples,
    collate_step_samples,
    load_prm,
    make_step_batches,
    step_bce_loss,
    step_metrics,
    step_prefixes,
)
from mini_verl.protocol import DataProto
from mini_verl.trainer.ppo_trainer import PPOTrainer


def _prm_config(tmp_path, *, total_steps: int = 60) -> PPOConfig:
    config = PPOConfig()
    config.trainer.logger = []
    config.trainer.seed = 3
    config.trainer.log_dir = str(tmp_path)
    config.trainer.experiment_name = "prm_unit"
    config.data.dataset = "copy"
    config.data.train_size = 64
    config.data.val_size = 16
    config.data.train_batch_size = 16
    config.data.max_prompt_length = 16
    config.data.max_response_length = 4
    config.reward.prm = {
        "enable": True,
        "arch": "mlp",
        "hidden_size": 32,
        "pooling": "mean",
        "step_tokens": 1,
        "label_mode": "local",
        "corruptions_per_sample": 1,
        "aggregation": "min",
        "lr": 0.02,
        "train_batch_size": 16,
        "total_steps": total_steps,
        "eval_freq": 10,
        "target_accuracy": 0.98,
        "patience": 4,
    }
    return config


class _LastTokenModel:
    """Deterministic prefix scorer: high logit iff the prefix ends in token 5."""

    max_seq_len = 64

    def __call__(self, input_ids, attention_mask):
        input_ids = np.asarray(input_ids)
        attention = np.asarray(attention_mask)
        positions = np.arange(input_ids.shape[1])[None, :]
        last = np.max(np.where(attention > 0, positions, -1), axis=1)
        tokens = input_ids[np.arange(input_ids.shape[0]), last]
        logits = np.where(tokens == 5, 10.0, -10.0).astype(np.float32)
        return Tensor(logits)


class _ContainsTokenModel:
    """High logit iff the prefix still contains a token from the prompt/earlier steps."""

    max_seq_len = 64

    def __init__(self, token: int = 5):
        self.token = token

    def __call__(self, input_ids, attention_mask):
        input_ids = np.asarray(input_ids)
        present = (input_ids == self.token).any(axis=1)
        logits = np.where(present, 10.0, -10.0).astype(np.float32)
        return Tensor(logits)


class _ConstantModel:
    max_seq_len = 64

    def __init__(self, logit: float = 10.0):
        self.logit = logit

    def __call__(self, input_ids, attention_mask):
        return Tensor(np.full(input_ids.shape[0], self.logit, dtype=np.float32))


# ----------------------------------------------------------------------
# data contract
# ----------------------------------------------------------------------


def test_build_step_samples_labels_prefixes_and_eos():
    tokenizer = CharTokenizer()
    dataset = build_dataset("copy", 4, seed=0)
    samples = build_step_samples(
        dataset,
        tokenizer,
        max_prompt_length=16,
        max_response_length=8,
        step_tokens=1,
        corruptions_per_sample=1,
        label_mode="local",
        seed=0,
    )
    # One positive and one corrupted trajectory per dataset sample.
    assert len(samples) == 2 * len(dataset)
    for index in range(0, len(samples), 2):
        positive, corrupted = samples[index], samples[index + 1]
        assert positive.labels == [1] * positive.num_steps
        assert positive.trajectory_label == 1
        assert sum(corrupted.labels) == corrupted.num_steps - 1
        assert corrupted.trajectory_label == 0
        # The supervised response includes EOS, exactly like a rollout.
        all_tokens = [token for step in positive.steps for token in step]
        assert all_tokens[-1] == tokenizer.eos_token_id
        # Prefixes are cumulative and end at each step boundary.
        prefixes = step_prefixes(positive)
        assert len(prefixes) == positive.num_steps
        assert prefixes[0] == positive.prompt_ids + positive.steps[0]
        assert prefixes[-1] == positive.prompt_ids + all_tokens


def test_suffix_labels_propagate_the_error():
    tokenizer = CharTokenizer()
    dataset = build_dataset("copy", 4, seed=0)
    samples = build_step_samples(
        dataset,
        tokenizer,
        max_prompt_length=16,
        max_response_length=8,
        step_tokens=1,
        corruptions_per_sample=1,
        label_mode="suffix",
        seed=0,
    )
    corrupted = samples[1]
    first_zero = corrupted.labels.index(0)
    assert all(label == 0 for label in corrupted.labels[first_zero:])
    assert all(label == 1 for label in corrupted.labels[:first_zero])
    assert corrupted.trajectory_label == 0


def test_step_sample_validation_and_build_arguments():
    with pytest.raises(ValueError):
        StepSample(prompt_ids=[1], steps=[], labels=[], uid=0, data_source="copy")
    with pytest.raises(ValueError):
        StepSample(prompt_ids=[1], steps=[[2]], labels=[1, 0], uid=0, data_source="copy")
    with pytest.raises(ValueError):
        StepSample(prompt_ids=[1], steps=[[2]], labels=[2], uid=0, data_source="copy")
    with pytest.raises(ValueError):
        StepSample(prompt_ids=[1], steps=[[]], labels=[1], uid=0, data_source="copy")
    dataset = build_dataset("copy", 4, seed=0)
    tokenizer = CharTokenizer()
    with pytest.raises(ValueError):
        build_step_samples(dataset, tokenizer, step_tokens=0)
    with pytest.raises(ValueError):
        build_step_samples(dataset, tokenizer, label_mode="unknown")
    with pytest.raises(ValueError):
        build_step_samples(dataset, tokenizer, corruptions_per_sample=-1)


def test_collate_and_batch_grouping():
    tokenizer = CharTokenizer()
    dataset = build_dataset("copy", 6, seed=0)
    samples = build_step_samples(dataset, tokenizer, max_response_length=4, seed=0)
    batch = collate_step_samples(samples[:3], tokenizer.pad_token_id)
    assert batch.num_groups == 3
    assert batch.group_index.tolist() == sorted(batch.group_index.tolist())
    assert len(batch) == sum(sample.num_steps for sample in samples[:3])
    assert batch.input_ids.shape == batch.attention_mask.shape
    assert batch.labels.shape == (len(batch),)
    batches = make_step_batches(samples, 4, tokenizer.pad_token_id, shuffle=False, drop_last=False)
    assert sum(item.num_groups for item in batches) == len(samples)
    # Whole trajectories stay inside one batch.
    for item in batches:
        assert item.num_groups <= 4


# ----------------------------------------------------------------------
# loss, aggregation, metrics
# ----------------------------------------------------------------------


def test_step_bce_loss_direction_stability_and_mask():
    correct = step_bce_loss(
        Tensor(np.asarray([2.0, 3.0], dtype=np.float32)),
        np.asarray([1.0, 1.0], dtype=np.float32),
    )
    wrong = step_bce_loss(
        Tensor(np.asarray([-2.0, -3.0], dtype=np.float32)),
        np.asarray([1.0, 1.0], dtype=np.float32),
    )
    assert correct.item() < wrong.item()
    zero = step_bce_loss(Tensor(np.zeros(4)), np.zeros(4)).item()
    assert zero == pytest.approx(np.log(2.0), abs=1e-6)
    extreme = Tensor(np.asarray([-500.0, 500.0], dtype=np.float32), requires_grad=True)
    loss = step_bce_loss(extreme, np.asarray([1.0, 0.0], dtype=np.float32))
    assert np.isfinite(loss.item())
    loss.backward()
    assert np.all(np.isfinite(extreme.grad))
    # A masked-out example must not change the mean over the kept examples.
    masked = step_bce_loss(
        Tensor(np.asarray([-2.0, 3.0], dtype=np.float32)),
        np.asarray([1.0, 1.0], dtype=np.float32),
        mask=np.asarray([0.0, 1.0], dtype=np.float32),
    )
    kept = step_bce_loss(Tensor(np.asarray([3.0], dtype=np.float32)), np.asarray([1.0]))
    assert masked.item() == pytest.approx(kept.item(), abs=1e-6)


def test_aggregation_methods():
    values = np.asarray([0.9, 0.8, 0.7], dtype=np.float64)
    assert aggregate_step_probs(values, "last") == pytest.approx(0.7)
    assert aggregate_step_probs(values, "first") == pytest.approx(0.9)
    assert aggregate_step_probs(values, "min") == pytest.approx(0.7)
    assert aggregate_step_probs(values, "max") == pytest.approx(0.9)
    assert aggregate_step_probs(values, "mean") == pytest.approx(0.8)
    assert aggregate_step_probs(values, "product") == pytest.approx(0.9 * 0.8 * 0.7)
    assert aggregate_step_probs([], "min") == 0.0
    assert set(AGGREGATIONS) == {"last", "first", "min", "max", "mean", "product"}
    with pytest.raises(ValueError):
        aggregate_step_probs(values, "median")
    with pytest.raises(ValueError):
        aggregate_step_probs([0.5, np.nan], "min")


def test_step_metrics_separates_step_and_trajectory_quality():
    probabilities = np.asarray([0.9, 0.9, 0.9, 0.1], dtype=np.float32)
    labels = np.asarray([1.0, 1.0, 1.0, 0.0], dtype=np.float32)
    groups = np.asarray([0, 0, 1, 1], dtype=np.int64)
    metrics = step_metrics(probabilities, labels, groups, method="min")
    assert metrics["steps"] == 4.0
    assert metrics["step/accuracy"] == pytest.approx(1.0)
    assert metrics["traj/accuracy"] == pytest.approx(1.0)
    assert metrics["traj/mean_score"] == pytest.approx((0.9 + 0.1) / 2)
    # last would call the second trajectory positive (0.1 is still below 0.5,
    # so use a clearer example).
    metrics_last = step_metrics(
        np.asarray([0.9, 0.9, 0.9, 0.8], dtype=np.float32),
        np.asarray([1.0, 1.0, 1.0, 0.0], dtype=np.float32),
        groups,
        method="last",
    )
    # ``last`` is fooled by the final locally-correct step; ``min`` is not.
    assert metrics_last["traj/accuracy"] == pytest.approx(0.5)


# ----------------------------------------------------------------------
# trainer and checkpoint
# ----------------------------------------------------------------------


def test_prm_trainer_learns_and_checkpoint_roundtrips(tmp_path):
    config = _prm_config(tmp_path, total_steps=80)
    trainer = PRMTrainer(config)
    result = trainer.fit()
    assert result["step_accuracy"] >= 0.85, result
    assert result["trajectory_accuracy"] >= 0.75, result
    assert result["brier"] < 0.2
    path = trainer.save(tmp_path / "prm")
    loaded, metadata = load_prm(path)
    assert metadata["format"] == "mini_verl.prm"
    assert metadata["step_tokens"] == 1
    batch = trainer.val_batches[0]
    with np.errstate(all="ignore"):
        before = trainer.model(batch.input_ids, batch.attention_mask).data
        after = loaded(batch.input_ids, batch.attention_mask).data
    assert np.allclose(before, after, atol=1e-6)

    # A scalar-RM checkpoint must not be accepted as a PRM checkpoint.
    (tmp_path / "wrong").mkdir()
    (tmp_path / "wrong" / "prm_config.json").write_text(
        json.dumps({"format": "mini_verl.reward_model", "vocab_size": 4}), encoding="utf-8"
    )
    (tmp_path / "wrong" / "prm_model.npz").write_bytes(b"")
    with pytest.raises(ValueError):
        load_prm(tmp_path / "wrong")


# ----------------------------------------------------------------------
# serving
# ----------------------------------------------------------------------


def _manager(model, **kwargs) -> ProcessRewardManager:
    options = {
        "aggregation": "min",
        "step_tokens": 1,
        "batch_size": 8,
        "metadata": {"max_seq_len": 64, "pad_token_id": 0},
    }
    options.update(kwargs)
    return ProcessRewardManager.from_model(model, **options)


def _data_with_response() -> tuple[DataProto, np.ndarray]:
    # prompt = [10, 11], response = [5, 6, 2] (content, content, EOS)
    input_ids = np.asarray([[10, 11, 5, 6, 2, 0, 0]], dtype=np.int64)
    attention = np.asarray([[1, 1, 1, 1, 1, 0, 0]], dtype=np.float32)
    response_mask = np.asarray([[0, 0, 1, 1, 1, 0, 0]], dtype=np.float32)
    data = DataProto(
        batch={
            "input_ids": input_ids,
            "attention_mask": attention,
            "response_mask": response_mask,
        },
        non_tensor_batch={},
        meta_info={"max_response_length": 3},
    )
    return data, response_mask


def test_process_manager_chunks_scores_and_scatters_last_token():
    # Checkpoint metadata is the default for serving, so omitting these keys
    # from the PPO config cannot silently change the segmentation.
    from_metadata = ProcessRewardManager.from_model(
        _LastTokenModel(),
        metadata={"max_seq_len": 64, "pad_token_id": 0, "aggregation": "product", "step_tokens": 3},
    )
    assert from_metadata.aggregation == "product"
    assert from_metadata.step_tokens == 3
    overridden = ProcessRewardManager.from_model(
        _LastTokenModel(),
        metadata={"max_seq_len": 64, "aggregation": "product", "step_tokens": 3},
        aggregation="min",
        step_tokens=1,
    )
    assert overridden.aggregation == "min" and overridden.step_tokens == 1

    # A prefix scorer that only looked at the current step token would fail
    # this: token 5 lives in the prompt, so every prefix must contain it.
    prefix_data = DataProto(
        batch={
            "input_ids": np.asarray([[10, 5, 6, 7, 2, 0]], dtype=np.int64),
            "attention_mask": np.asarray([[1, 1, 1, 1, 1, 0]], dtype=np.float32),
            "response_mask": np.asarray([[0, 0, 1, 1, 1, 0]], dtype=np.float32),
        },
        non_tensor_batch={},
        meta_info={"max_response_length": 3},
    )
    prefix_manager = _manager(_ContainsTokenModel(5), aggregation="first")
    prefix_scored = prefix_manager.run(prefix_data)
    prefix_steps = prefix_scored.non_tensor_batch["prm_step_scores"][0]
    assert len(prefix_steps) == 3
    assert all(value > 0.99 for value in prefix_steps), prefix_steps

    manager = _manager(_LastTokenModel(), aggregation="min")
    data, response_mask = _data_with_response()
    scored = manager.run(data)
    step_scores = scored.non_tensor_batch["prm_step_scores"][0]
    assert len(step_scores) == 3
    # Only the first prefix ends in token 5.
    assert step_scores[0] > 0.99
    assert step_scores[1] < 0.01 and step_scores[2] < 0.01
    expected = float(np.min(step_scores))
    assert scored.batch["rm_scores"][0] == pytest.approx(expected, abs=1e-6)
    assert scored.batch["prm_scores"][0] == pytest.approx(expected, abs=1e-6)
    rewards = scored.batch["token_level_rewards"]
    assert rewards[0, 4] == pytest.approx(expected, abs=1e-6)
    assert np.count_nonzero(rewards) == 1
    assert response_mask[0].sum() == 3
    assert scored.meta_info["prm/aggregation"] == "min"
    assert scored.meta_info["prm/mean_steps"] == pytest.approx(3.0)


def test_process_manager_honours_explicit_boundaries_and_rejects_bad_ones():
    manager = _manager(_LastTokenModel(), step_tokens=1)
    data, _ = _data_with_response()
    data.non_tensor_batch["step_boundaries"] = np.asarray([[0, 2]], dtype=object)
    scored = manager.run(data)
    assert len(scored.non_tensor_batch["prm_step_scores"][0]) == 2
    # First boundary ends at token 5 (high), second at EOS (low).
    assert scored.non_tensor_batch["prm_step_scores"][0][0] > 0.99
    assert scored.non_tensor_batch["prm_step_scores"][0][1] < 0.01

    data, _ = _data_with_response()
    data.non_tensor_batch["step_boundaries"] = np.asarray([[0, 1]], dtype=object)
    with pytest.raises(ValueError, match="last boundary"):
        manager.run(data)

    data, _ = _data_with_response()
    data.non_tensor_batch["step_boundaries"] = np.asarray([[1, 1]], dtype=object)
    with pytest.raises(ValueError, match="strictly increasing"):
        manager.run(data)

    data, _ = _data_with_response()
    data.batch["response_mask"] = np.zeros_like(data.batch["response_mask"])
    data.non_tensor_batch["step_boundaries"] = np.asarray([[0]], dtype=object)
    with pytest.raises(ValueError, match="no valid response tokens"):
        manager.run(data)


def test_process_manager_overlong_shaping_and_clone():
    manager = _manager(_ConstantModel(50.0), overlong_penalty_factor=0.5)
    data, _ = _data_with_response()
    data.batch["truncated"] = np.asarray([1.0], dtype=np.float32)
    scored = manager.run(data)
    # 1.0 - 0.5 * (3 / 3) = 0.5
    assert scored.batch["rm_scores"][0] == pytest.approx(0.5, abs=1e-6)
    clone = manager.without_shaping()
    assert clone.overlong_penalty_factor == 0.0
    assert clone.aggregation == manager.aggregation
    unscored = clone.run(data.copy())
    assert unscored.batch["rm_scores"][0] == pytest.approx(1.0, abs=1e-6)


def test_process_manager_combines_prm_shaping_with_a_verifier():
    manager = _manager(
        _ConstantModel(50.0),
        outcome_reward_fn="exact_match",
        outcome_weight=1.0,
        prm_weight=0.1,
    )
    data, _ = _data_with_response()
    data.non_tensor_batch["response_text"] = np.asarray(["ax"], dtype=object)
    data.non_tensor_batch["answers"] = np.asarray(["ax"], dtype=object)
    scored = manager.run(data)
    assert scored.batch["prm_trajectory_scores"][0] == pytest.approx(1.0, abs=1e-6)
    assert scored.batch["prm_outcome_scores"][0] == pytest.approx(1.0)
    assert scored.batch["prm_scores"][0] == pytest.approx(1.0, abs=1e-6)
    assert scored.batch["rm_scores"][0] == pytest.approx(1.1, abs=1e-6)
    assert scored.meta_info["prm/outcome_weight"] == 1.0
    assert scored.meta_info["prm/prm_weight"] == 0.1

    wrong = _data_with_response()[0]
    wrong.non_tensor_batch["response_text"] = np.asarray(["zz"], dtype=object)
    wrong.non_tensor_batch["answers"] = np.asarray(["ax"], dtype=object)
    scored_wrong = manager.run(wrong)
    assert scored_wrong.batch["prm_outcome_scores"][0] == 0.0
    assert scored_wrong.batch["rm_scores"][0] == pytest.approx(0.1, abs=1e-6)

    missing = _data_with_response()[0]
    with pytest.raises(KeyError, match="response_text"):
        manager.run(missing)
    with pytest.raises(ValueError, match="outcome_weight"):
        ProcessRewardManager.from_model(
            _ConstantModel(), metadata={"max_seq_len": 64}, outcome_weight=1.0
        )
    with pytest.raises(ValueError, match="outcome_reward_fn requires"):
        ProcessRewardManager.from_model(
            _ConstantModel(),
            metadata={"max_seq_len": 64},
            outcome_reward_fn="exact_match",
        )


def test_ppo_rejects_prm_without_model_path_and_selects_manager(tmp_path):
    config = PPOConfig()
    config.trainer.logger = []
    config.trainer.seed = 1
    config.trainer.val_before_train = False
    config.trainer.total_training_steps = 1
    config.trainer.log_dir = str(tmp_path)
    config.trainer.experiment_name = "prm_ppo"
    config.data.dataset = "copy"
    config.data.train_size = 8
    config.data.val_size = 4
    config.data.train_batch_size = 8
    config.data.filter_overlong_prompts = False
    config.data.max_prompt_length = 8
    config.data.max_response_length = 3
    config.actor_rollout_ref.rollout.n = 2
    config.actor_rollout_ref.actor.ppo_mini_batch_size = 16
    config.reward.prm = {"enable": True}
    with pytest.raises(ValueError, match="model_path"):
        PPOTrainer(config).init()
    config.reward.reward_model = {"enable": True, "model_path": "unused"}
    with pytest.raises(ValueError, match="only one of"):
        PPOTrainer(config).init()
    config.reward.reward_model = {"enable": False}

    trainer_config = _prm_config(tmp_path, total_steps=40)
    prm = PRMTrainer(trainer_config)
    prm.fit()
    checkpoint = prm.save(tmp_path / "prm_ppo_checkpoint")
    config.reward.prm = {
        "enable": True,
        "model_path": str(checkpoint),
        "aggregation": "min",
        "step_tokens": 1,
    }
    trainer = PPOTrainer(config).init()
    assert isinstance(trainer.reward_manager, ProcessRewardManager)
    result = trainer.fit()
    assert result["steps"] == 1
    assert np.isfinite(result["history"]["train/reward_mean"])
